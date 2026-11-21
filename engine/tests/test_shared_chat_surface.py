import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

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

pytest_plugins = ("test_chat_submission_postgres",)


@pytest.mark.parametrize("operation", ["read", "list_all", "rename", "delete"])
async def test_another_users_canonical_conversation_grants_a_member_no_read_or_mutation_rights(monkeypatch, operation):
    viewer = UserRecord(id=uuid4())
    agent = AgentRecord(id=uuid4(), name="Agent", creator_id=uuid4())
    session = ChatSessionRecord(id=uuid4(), agent_id=agent.id, user_id=uuid4(), source_channel="web", is_primary=True)

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

    if operation == "read":
        action = chat_sessions._authorize_history(agent.id, session.id, viewer)
    elif operation == "list_all":
        action = chat_sessions.list_sessions(agent.id, "all", viewer)
    elif operation == "rename":
        action = chat_sessions.rename_session(
            agent.id, session.id, chat_sessions.PatchSessionIn(title="changed"), viewer
        )
    else:
        action = chat_sessions.delete_session(agent.id, session.id, viewer)
    with pytest.raises(HTTPException) as error:
        await action

    assert error.value.status_code == 403
    update.assert_not_awaited()
    delete.assert_not_awaited()


def _chat_app(conninfo: str, current_user) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await init_pool(conninfo=conninfo, min_size=1, max_size=4)
        try:
            yield
        finally:
            await close_pool()

    app = FastAPI(lifespan=lifespan)
    app.include_router(websocket.router)
    app.include_router(chat_sessions.router)
    app.dependency_overrides[get_current_user] = current_user
    return app


async def test_two_humans_each_get_a_private_conversation_over_rest_and_socket(receipt_db, monkeypatch):
    db = receipt_db
    other = UserRecord(id=uuid4(), quota_message_limit=1)
    async with transaction(db.first):
        await db.first.execute(
            "INSERT INTO users (id, display_name, quota_message_limit) VALUES (%(id)s, 'Other', 1)",
            {"id": other.id},
        )
    users = {db.user.id: db.user, other.id: other}
    caller = {"user": db.user}
    bindings: dict[str, chat_tickets.ChatTicketBinding] = {}

    def bind(user: UserRecord, session_id: str) -> chat_tickets.ChatTicketBinding:
        return chat_tickets.ChatTicketBinding(
            user_id=user.id,
            tenant_id=uuid4(),
            agent_id=db.agent.id,
            session_id=UUID(session_id),
            auth_deadline=int(time.time()) + 3600,
            auth_generation=0,
        )

    monkeypatch.setattr(chat_tickets, "redeem", AsyncMock(side_effect=lambda ticket: bindings[ticket]))
    monkeypatch.setattr(
        chat_tickets,
        "authorize",
        AsyncMock(side_effect=lambda binding, *_a, **_k: (users[binding.user_id], db.agent, "use")),
    )
    monkeypatch.setattr(websocket, "manager", websocket.ConnectionManager())
    monkeypatch.setattr(websocket.realtime_router, "register_connection", AsyncMock())
    monkeypatch.setattr(websocket.realtime_router, "unregister_connection", AsyncMock())
    monkeypatch.setattr(websocket.realtime_router, "_list_presence", AsyncMock(return_value=[]))
    app = _chat_app(db.conninfo, lambda: caller["user"])
    sessions_path = f"/api/agents/{db.agent.id}/sessions"

    with TestClient(app) as client:
        mine = client.post(sessions_path, json={"title": "ignored"})
        assert mine.status_code == 201
        assert client.post(sessions_path, json={}).json()["id"] == mine.json()["id"]
        caller["user"] = other
        theirs = client.post(sessions_path, json={})
        assert theirs.status_code == 201
        assert theirs.json()["id"] != mine.json()["id"]
        for created, owner in ((mine.json(), db.user), (theirs.json(), other)):
            assert (created["user_id"], created["source_channel"], created["writable"]) == (
                str(owner.id),
                "web",
                True,
            )

        for ticket, owner, created, content in (
            ("first", db.user, mine.json(), "first human"),
            ("second", other, theirs.json(), "second human"),
        ):
            bindings[ticket] = bind(owner, created["id"])
            submission = {"content": content, "client_submission_id": str(uuid4())}
            path = f"/ws/chat/{db.agent.id}?session_id={created['id']}&ticket={ticket}"
            with client.websocket_connect(path) as socket:
                assert socket.receive_json()["type"] == "connected"
                socket.send_json(submission)
                accepted = socket.receive_json()
                assert accepted["type"] == "turn_accepted"
                assert accepted["session_id"] == created["id"]

        caller["user"] = db.user
        history = client.get(f"{sessions_path}/{mine.json()['id']}/messages")
        assert [row["content"] for row in history.json()] == ["first human"]
        assert client.delete(f"{sessions_path}/{mine.json()['id']}").json()["detail"] == {
            "reason": "canonical_session_required",
            "session_id": mine.json()["id"],
        }
        assert [row["id"] for row in client.get(sessions_path).json()] == [mine.json()["id"]]

        caller["user"] = other
        assert client.get(f"{sessions_path}/{mine.json()['id']}/messages").status_code == 403
        history = client.get(f"{sessions_path}/{theirs.json()['id']}/messages")
        assert [row["content"] for row in history.json()] == ["second human"]
        assert [row["id"] for row in client.get(sessions_path).json()] == [theirs.json()["id"]]


@pytest.mark.parametrize("kind", ["submission", "onboarding_trigger"])
async def test_read_only_socket_reads_and_replays_but_rejects_new_writes(receipt_db, monkeypatch, kind):
    db = receipt_db
    submission = ChatSubmission(client_submission_id=uuid4(), content="earlier message")
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
    prepare = AsyncMock(side_effect=AssertionError("A read-only session must not prepare a new turn"))
    monkeypatch.setattr(websocket, "prepare_openclaw_message", prepare)
    app = _chat_app(db.conninfo, lambda: db.user)
    path = f"/ws/chat/{db.agent.id}?session_id={db.session_id}&ticket=read-only"

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
        refusal = socket.receive_json()

    async with transaction(db.second):
        canonical = await db.second.fetchone(
            "SELECT id FROM chat_sessions WHERE agent_id = %(agent)s AND user_id = %(user)s "
            "AND source_channel = 'web' AND is_primary AND is_group IS NOT TRUE",
            {"agent": db.agent.id, "user": db.user.id},
        )
    assert canonical is not None
    assert canonical["id"] != db.session_id
    assert refusal == {
        "type": "error",
        "content": {"reason": "session_read_only", "session_id": str(canonical["id"])},
    }
    prepare.assert_not_awaited()
