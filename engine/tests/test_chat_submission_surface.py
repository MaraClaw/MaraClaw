import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from app.api import chat_sessions, websocket
from app.core.domain.users import UserRole
from app.core.json_types import JsonObject, json_loads_object
from app.core.security import get_current_user
from app.db.pool import close_pool, init_pool
from app.db.session import transaction
from app.records.agent import AgentRecord
from app.records.chat import ChatMessageRecord, ChatSessionRecord
from app.records.user import UserRecord
from app.services import chat_submissions, chat_tickets
from app.services.chat_submissions import ChatSubmission
from app.services.shared_chat import shared_session_id
from app.services.storage_runtime import LocalStorageBackend

pytest_plugins = ("test_chat_submission_postgres",)


@pytest.mark.parametrize("source", ["web", "agent", "shared"])
@pytest.mark.parametrize("viewer", ["owner", "member", "creator", "platform_admin", "org_admin", "agent_admin"])
async def test_locked_submission_replay_access_matrix(monkeypatch, source, viewer):
    user = UserRecord(id=uuid4(), role=UserRole(viewer) if viewer not in ("owner", "creator") else UserRole.MEMBER)
    agent = AgentRecord(id=uuid4(), name="Agent", creator_id=user.id if viewer == "creator" else uuid4())
    session = ChatSessionRecord(
        id=shared_session_id(agent.id) if source == "shared" else uuid4(),
        agent_id=agent.id,
        user_id=user.id if viewer == "owner" else uuid4(),
        source_channel=source,
    )
    submission = ChatSubmission(client_submission_id=uuid4(), content="existing receipt")
    receipt = ChatMessageRecord(
        id=uuid4(),
        user_id=user.id,
        agent_id=agent.id,
        role="user",
        content=submission.content,
        conversation_id=str(session.id),
        client_submission_id=submission.client_submission_id,
        submission_fingerprint=submission.fingerprint(),
    )
    state = SimpleNamespace(in_transaction=False, locked=False)

    @asynccontextmanager
    async def connection():
        state.in_transaction = True
        try:
            yield
        finally:
            state.in_transaction = False

    async def lock(session_id, agent_id):
        assert state.in_transaction
        assert (session_id, agent_id) == (session.id, agent.id)
        state.locked = True
        return session

    async def find(_candidate):
        assert state.locked
        assert state.in_transaction
        return receipt

    find_receipt = AsyncMock(side_effect=find)
    prepare = AsyncMock(side_effect=AssertionError("Replay must not prepare a turn"))
    monkeypatch.setattr(chat_submissions, "connection_ctx", connection)
    monkeypatch.setattr(chat_submissions.chat_session_dao, "lock_for_agent", lock)
    monkeypatch.setattr(chat_submissions.chat_message_dao, "find_submission", find_receipt)
    monkeypatch.setattr(chat_submissions, "prepare_openclaw_message", prepare)

    if viewer == "member" and source != "shared":
        with pytest.raises(HTTPException) as error:
            await chat_submissions.accept_submission(
                submission, user=user, agent=agent, session_id=session.id, history=[]
            )
        assert error.value.status_code == 403
        find_receipt.assert_not_awaited()
    else:
        accepted = await chat_submissions.accept_submission(
            submission, user=user, agent=agent, session_id=session.id, history=[]
        )
        assert accepted.replayed
        assert accepted.message.id == receipt.id
    assert state.locked
    prepare.assert_not_awaited()


@pytest.mark.parametrize("source", ["web", "agent", "shared"])
@pytest.mark.parametrize("viewer", ["owner", "member", "creator", "platform_admin", "org_admin", "agent_admin"])
async def test_new_submission_access_preserves_archive_write_guard(monkeypatch, source, viewer):
    user = UserRecord(id=uuid4(), role=UserRole(viewer) if viewer not in ("owner", "creator") else UserRole.MEMBER)
    agent = AgentRecord(id=uuid4(), name="Agent", creator_id=user.id if viewer == "creator" else uuid4())
    session = ChatSessionRecord(
        id=shared_session_id(agent.id) if source == "shared" else uuid4(),
        agent_id=agent.id,
        user_id=user.id if viewer == "owner" else uuid4(),
        source_channel=source,
    )

    @asynccontextmanager
    async def connection():
        yield

    async def reserve(candidate):
        return candidate

    prepare = AsyncMock()
    charge = AsyncMock()
    enqueue = AsyncMock()
    monkeypatch.setattr(chat_submissions, "connection_ctx", connection)
    monkeypatch.setattr(chat_submissions.chat_session_dao, "lock_for_agent", AsyncMock(return_value=session))
    monkeypatch.setattr(chat_submissions.chat_session_dao, "update", AsyncMock(return_value=session))
    monkeypatch.setattr(chat_submissions.chat_message_dao, "find_submission", AsyncMock(return_value=None))
    monkeypatch.setattr(chat_submissions.chat_message_dao, "list_recent", AsyncMock(return_value=[]))
    monkeypatch.setattr(chat_submissions.chat_message_dao, "reserve_submission", reserve)
    monkeypatch.setattr(chat_submissions, "check_agent_expired", AsyncMock())
    monkeypatch.setattr(chat_submissions, "prepare_openclaw_message", prepare)
    monkeypatch.setattr(chat_submissions, "charge_conversation_quota", charge)
    monkeypatch.setattr(chat_submissions, "enqueue_prepared_openclaw_message", enqueue)
    submission = ChatSubmission(client_submission_id=uuid4(), content="new message")

    if source == "web" or (source == "agent" and viewer == "member"):
        with pytest.raises(HTTPException) as error:
            await chat_submissions.accept_submission(
                submission, user=user, agent=agent, session_id=session.id, history=[]
            )
        assert error.value.status_code == (403 if viewer == "member" else 409)
        prepare.assert_not_awaited()
        charge.assert_not_awaited()
        enqueue.assert_not_awaited()
    else:
        accepted = await chat_submissions.accept_submission(
            submission, user=user, agent=agent, session_id=session.id, history=[]
        )
        assert not accepted.replayed
        assert (accepted.message.user_id, accepted.message.conversation_id) == (user.id, str(session.id))
        charge.assert_awaited_once_with(user)
        enqueue.assert_awaited_once_with(
            prepare.return_value, sender_user_id=user.id, conversation_id=str(session.id), deduplicate=False
        )


@pytest.mark.parametrize("with_attachments", [False, True])
async def test_socket_receipt_reconnect_history_conflict_and_delete(
    writable_receipt_db, monkeypatch, tmp_path, with_attachments
):
    db = writable_receipt_db
    binding = chat_tickets.ChatTicketBinding(
        user_id=db.user.id,
        tenant_id=uuid4(),
        agent_id=db.agent.id,
        session_id=db.session_id,
        auth_deadline=int(time.time()) + 3600,
        auth_generation=0,
    )
    monkeypatch.setattr(chat_tickets, "redeem", AsyncMock(return_value=binding))
    monkeypatch.setattr(chat_tickets, "authorize", AsyncMock(return_value=(db.user, db.agent, "manage")))
    monkeypatch.setattr(websocket.manager, "connect", AsyncMock())
    monkeypatch.setattr(websocket.manager, "disconnect", AsyncMock())
    monkeypatch.setattr(websocket.manager, "send_to_session", AsyncMock())

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await init_pool(conninfo=db.conninfo, min_size=1, max_size=2)
        try:
            yield
        finally:
            await close_pool()

    app = FastAPI(lifespan=lifespan)
    app.include_router(websocket.router)
    app.include_router(chat_sessions.router)
    app.dependency_overrides[get_current_user] = lambda: db.user
    socket_path = f"/ws/chat/{db.agent.id}?ticket=qa&session_id={db.session_id}"
    session_path = f"/api/agents/{db.agent.id}/sessions/{db.session_id}"
    submission_id = str(uuid4())
    envelope: dict[str, object] = {
        "content": "hello",
        "display_content": "visible",
        "client_submission_id": submission_id,
    }
    refs = [{"path": "workspace/uploads/report.docx", "name": "Report.docx"}]
    if with_attachments:
        file = tmp_path / str(db.agent.id) / refs[0]["path"]
        file.parent.mkdir(parents=True)
        file.write_bytes(b"file reference only")
        monkeypatch.setattr(chat_submissions, "get_storage_backend", lambda: LocalStorageBackend(str(tmp_path)))
        envelope["attachments"] = refs

    with TestClient(app) as client:
        with client.websocket_connect(socket_path) as socket:
            assert socket.receive_json()["type"] == "connected"
            socket.send_json(envelope)
            accepted = json_loads_object(socket.receive_text())
            assert accepted["type"] == "turn_accepted"
            assert accepted["session_id"] == str(db.session_id)
            assert accepted["client_submission_id"] == submission_id
            assert socket.receive_json() == {"type": "status", "content": "thinking"}

        with client.websocket_connect(socket_path) as socket:
            assert socket.receive_json()["type"] == "connected"
            socket.send_json(envelope)
            assert socket.receive_json() == accepted
            socket.send_json({**envelope, "content": ""})
            conflict = json_loads_object(socket.receive_text())
            assert conflict["reason"] == "submission_conflict"
            assert conflict["client_submission_id"] == submission_id
            socket.send_json({"content": "hello"})
            assert socket.receive_json()["reason"] == "invalid_submission_id"

            history = client.get(session_path + "/messages")
            assert history.status_code == 200
            rows = TypeAdapter(list[JsonObject]).validate_json(history.content)
            assert [(row["message_id"], row["client_submission_id"]) for row in rows] == [
                (accepted["message_id"], submission_id)
            ]
            if with_attachments:
                assert rows[0]["attachments"] == refs
            else:
                assert "attachments" not in rows[0]
            assert client.delete(session_path).status_code == 409
            socket.send_json(envelope)
            assert socket.receive_json() == accepted
        assert client.get(session_path + "/messages").status_code == 200


async def test_enqueue_failure_rolls_back_receipt_charge_session_and_gateway(writable_receipt_db, monkeypatch):
    db = writable_receipt_db
    original_enqueue = chat_submissions.enqueue_prepared_openclaw_message

    async def fail_after_enqueue(prepared, *, sender_user_id, conversation_id, deduplicate):
        await original_enqueue(
            prepared, sender_user_id=sender_user_id, conversation_id=conversation_id, deduplicate=deduplicate
        )
        raise RuntimeError("injected after durable enqueue")

    monkeypatch.setattr(chat_submissions, "enqueue_prepared_openclaw_message", fail_after_enqueue)
    with pytest.raises(RuntimeError, match="injected after durable enqueue"):
        async with transaction(db.first):
            await chat_submissions.accept_submission(
                ChatSubmission(client_submission_id=uuid4(), content="hello"),
                user=db.user,
                agent=db.agent,
                session_id=db.session_id,
                history=[],
            )

    async with transaction(db.second):
        stored = await db.second.fetchone(
            "SELECT (SELECT count(*) FROM chat_messages) AS messages, "
            "(SELECT count(*) FROM gateway_messages) AS enqueued, "
            "(SELECT quota_messages_used FROM users WHERE id = %(user)s) AS charged, "
            "title, last_message_at FROM chat_sessions WHERE id = %(session)s",
            {"user": db.user.id, "session": db.session_id},
        )
    assert stored == {"messages": 0, "enqueued": 0, "charged": 0, "title": "Session QA", "last_message_at": None}


async def test_failed_session_delete_retains_receipt_and_parent(receipt_db, monkeypatch):
    db = receipt_db
    async with transaction(db.first):
        await db.first.execute(
            "UPDATE chat_sessions SET source_channel = 'agent' WHERE id = %(id)s", {"id": db.session_id}
        )
        accepted = await chat_submissions.accept_submission(
            ChatSubmission(client_submission_id=uuid4(), content="hello"),
            user=db.user,
            agent=db.agent,
            session_id=db.session_id,
            history=[],
        )
    monkeypatch.setattr(chat_sessions.chat_session_dao, "delete", AsyncMock(side_effect=RuntimeError("delete failed")))

    with pytest.raises(RuntimeError, match="delete failed"):
        async with transaction(db.first):
            await chat_sessions.delete_session(db.agent.id, db.session_id, db.user)

    async with transaction(db.second):
        retained = await db.second.fetchone(
            "SELECT (SELECT count(*) FROM chat_sessions) AS sessions, id, client_submission_id "
            "FROM chat_messages WHERE id = %(id)s",
            {"id": accepted.message.id},
        )
    assert retained == {
        "sessions": 1,
        "id": accepted.message.id,
        "client_submission_id": accepted.message.client_submission_id,
    }
