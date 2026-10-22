import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from app.api import chat_sessions, websocket
from app.core.json_types import JsonObject, json_loads_object
from app.core.security import get_current_user
from app.db.pool import close_pool, init_pool
from app.db.session import transaction
from app.services import chat_submissions, chat_tickets
from app.services.chat_submissions import ChatSubmission

pytest_plugins = ("test_chat_submission_postgres",)


async def test_socket_receipt_reconnect_history_conflict_and_delete(writable_receipt_db, monkeypatch):
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
    envelope = {"content": "hello", "display_content": "visible", "client_submission_id": submission_id}

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
