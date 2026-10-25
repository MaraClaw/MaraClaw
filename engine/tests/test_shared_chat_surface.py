import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import chat_sessions, websocket
from app.core.security import get_current_user
from app.dao.chat_dao import chat_message_dao
from app.db.pool import close_pool, init_pool
from app.db.session import transaction
from app.records.agent import AgentRecord
from app.records.chat import ChatMessageRecord, ChatSessionRecord
from app.records.user import UserRecord
from app.services import chat_tickets
from app.services.chat_submissions import ChatSubmission
from app.services.shared_chat import ensure_shared_session, shared_session_id

pytest_plugins = ("test_chat_submission_postgres",)


@pytest.mark.parametrize("operation", ["list_all", "rename", "delete"])
async def test_shared_visibility_does_not_grant_mutation_or_audit_rights(monkeypatch, operation):
    viewer = UserRecord(id=uuid4())
    agent = AgentRecord(id=uuid4(), name="Agent", creator_id=uuid4())
    session = ChatSessionRecord(
        id=shared_session_id(agent.id), agent_id=agent.id, user_id=agent.creator_id, source_channel="shared"
    )

    @asynccontextmanager
    async def connection():
        yield

    update = AsyncMock()
    delete = AsyncMock()
    monkeypatch.setattr(chat_sessions, "check_agent_access", AsyncMock(return_value=(agent, "use")))
    monkeypatch.setattr(chat_sessions.agent_dao, "get", AsyncMock(return_value=agent))
    monkeypatch.setattr(chat_sessions, "connection_ctx", connection)
    monkeypatch.setattr(chat_sessions.chat_session_dao, "get_for_agent_or_peer", AsyncMock(return_value=session))
    monkeypatch.setattr(chat_sessions.chat_session_dao, "get_for_agent", AsyncMock(return_value=session))
    monkeypatch.setattr(chat_sessions.chat_session_dao, "lock_for_agent", AsyncMock(return_value=session))
    monkeypatch.setattr(chat_sessions.chat_session_dao, "update", update)
    monkeypatch.setattr(chat_sessions.chat_session_dao, "delete", delete)

    assert await chat_sessions._authorize_history(agent.id, session.id, viewer) == session
    if operation == "list_all":
        action = chat_sessions.list_sessions(agent.id, "all", viewer)
    elif operation == "rename":
        action = chat_sessions.rename_session(
            agent.id, session.id, chat_sessions.PatchSessionIn(title="changed"), viewer
        )
    else:
        action = chat_sessions.delete_session(agent.id, session.id, viewer)
    with pytest.raises(HTTPException) as error:
        await action

    assert error.value.status_code == (409 if operation == "delete" else 403)
    update.assert_not_awaited()
    delete.assert_not_awaited()


async def test_two_humans_share_live_transcript_replay_and_http_history(receipt_db, monkeypatch):
    db = receipt_db
    viewer = UserRecord(id=uuid4(), quota_message_limit=1)
    async with transaction(db.first):
        await db.first.execute(
            "INSERT INTO users (id, display_name, quota_message_limit) VALUES (%(id)s, 'Viewer', 1)",
            {"id": viewer.id},
        )
        shared = await ensure_shared_session(db.agent.id)
    bindings = {
        token: chat_tickets.ChatTicketBinding(
            user_id=user.id,
            tenant_id=uuid4(),
            agent_id=db.agent.id,
            session_id=shared.id,
            auth_deadline=int(time.time()) + 3600,
            auth_generation=0,
        )
        for token, user in (("sender", db.user), ("viewer", viewer))
    }

    async def redeem(ticket):
        return bindings[ticket]

    async def authorize(binding):
        return (db.user if binding.user_id == db.user.id else viewer), db.agent, "use"

    monkeypatch.setattr(chat_tickets, "redeem", redeem)
    monkeypatch.setattr(chat_tickets, "authorize", authorize)
    monkeypatch.setattr(websocket, "manager", websocket.ConnectionManager())
    monkeypatch.setattr(websocket.realtime_router, "register_connection", AsyncMock())
    monkeypatch.setattr(websocket.realtime_router, "unregister_connection", AsyncMock())
    monkeypatch.setattr(websocket.realtime_router, "_list_presence", AsyncMock(return_value=[]))

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await init_pool(conninfo=db.conninfo, min_size=1, max_size=4)
        try:
            yield
        finally:
            await close_pool()

    app = FastAPI(lifespan=lifespan)
    app.include_router(websocket.router)
    app.include_router(chat_sessions.router)
    app.dependency_overrides[get_current_user] = lambda: viewer
    socket_path = f"/ws/chat/{db.agent.id}?session_id={shared.id}&ticket="
    session_path = f"/api/agents/{db.agent.id}/sessions"
    first = {"content": "first human", "client_submission_id": str(uuid4())}
    second = {"content": "second human", "client_submission_id": str(uuid4())}

    with TestClient(app) as client, client.websocket_connect(socket_path + "sender") as sender:
        assert sender.receive_json()["type"] == "connected"
        with client.websocket_connect(socket_path + "viewer") as observer:
            assert observer.receive_json()["type"] == "connected"
            sender.send_json(first)
            accepted = sender.receive_json()
            assert accepted["type"] == "turn_accepted"
            completion = sender.receive_json()
            assert completion["message_id"] == accepted["message_id"]
            assert completion["role"] == "user"
            assert "entry_id" not in completion
            assert observer.receive_json() == completion
            assert sender.receive_json() == {"type": "status", "content": "thinking"}

            sender.send_json(first)
            assert sender.receive_json() == accepted
            sender.send_json({"content": "bad UUID"})
            assert sender.receive_json()["reason"] == "invalid_submission_id"

            observer.send_json(second)
            reply = observer.receive_json()
            assert reply["type"] == "turn_accepted"
            reply_completion = observer.receive_json()
            assert reply_completion["message_id"] == reply["message_id"]
            assert sender.receive_json() == reply_completion
            assert observer.receive_json() == {"type": "status", "content": "thinking"}

            history = client.get(f"{session_path}/{shared.id}/messages")
            assert history.status_code == 200
            assert [row["content"] for row in history.json()] == ["first human", "second human"]
            resumed = client.post(session_path, json={"title": "ignored"})
            assert resumed.status_code == 201
            assert resumed.json()["id"] == str(shared.id)
            assert resumed.json()["message_count"] == 2
            assert resumed.json()["unread_count"] == 0
            assert client.delete(f"{session_path}/{shared.id}").status_code == 409


@pytest.mark.parametrize("kind", ["submission", "onboarding_trigger"])
async def test_archive_socket_reads_and_replays_but_rejects_new_writes(receipt_db, monkeypatch, kind):
    db = receipt_db
    submission = ChatSubmission(client_submission_id=uuid4(), content="archived message")
    original = ChatMessageRecord(
        id=uuid4(),
        agent_id=db.agent.id,
        user_id=db.user.id,
        role="user",
        content=submission.content,
        conversation_id=str(db.session_id),
        client_submission_id=submission.client_submission_id,
        submission_fingerprint=submission.fingerprint(),
    )
    async with transaction(db.first):
        await chat_message_dao.reserve_submission(original)
    binding = chat_tickets.ChatTicketBinding(
        user_id=db.user.id,
        tenant_id=uuid4(),
        agent_id=db.agent.id,
        session_id=db.session_id,
        auth_deadline=int(time.time()) + 3600,
        auth_generation=0,
    )
    monkeypatch.setattr(chat_tickets, "redeem", AsyncMock(return_value=binding))
    monkeypatch.setattr(chat_tickets, "authorize", AsyncMock(return_value=(db.user, db.agent, "use")))
    monkeypatch.setattr(websocket, "manager", websocket.ConnectionManager())
    monkeypatch.setattr(websocket.realtime_router, "register_connection", AsyncMock())
    monkeypatch.setattr(websocket.realtime_router, "unregister_connection", AsyncMock())
    monkeypatch.setattr(websocket, "is_onboarded", AsyncMock(return_value=False))
    prepare = AsyncMock(side_effect=AssertionError("Archive must not prepare a new turn"))
    monkeypatch.setattr(websocket, "prepare_openclaw_message", prepare)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await init_pool(conninfo=db.conninfo, min_size=1, max_size=4)
        try:
            yield
        finally:
            await close_pool()

    app = FastAPI(lifespan=lifespan)
    app.include_router(websocket.router)
    app.include_router(chat_sessions.router)
    app.dependency_overrides[get_current_user] = lambda: db.user
    path = f"/ws/chat/{db.agent.id}?session_id={db.session_id}&ticket=archive"
    with TestClient(app) as client, client.websocket_connect(path) as socket:
        assert socket.receive_json()["type"] == "connected"
        history = client.get(f"/api/agents/{db.agent.id}/sessions/{db.session_id}/messages")
        assert history.status_code == 200
        assert [row["content"] for row in history.json()] == [submission.content]
        socket.send_json(submission.model_dump(mode="json"))
        assert socket.receive_json() == {
            "type": "turn_accepted",
            "message_id": str(original.id),
            "session_id": str(db.session_id),
            "client_submission_id": str(submission.client_submission_id),
        }
        socket.send_json({"content": "new write", "client_submission_id": str(uuid4()), "kind": kind})
        assert socket.receive_json() == {
            "type": "error",
            "content": {"reason": "session_archived", "session_id": str(shared_session_id(db.agent.id))},
        }
    prepare.assert_not_awaited()
