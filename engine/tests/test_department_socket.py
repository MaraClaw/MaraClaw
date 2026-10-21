import json
import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from starlette.websockets import WebSocket

from app.api import websocket as ws_api
from app.core import access_cache, permissions, security
from app.records.agent import AgentRecord
from app.records.chat import ChatMessageRecord, ChatSessionRecord
from app.records.identity import IdentityRecord
from app.records.user import UserRecord
from app.services import chat_submissions, chat_tickets
from app.services.shared_chat import shared_session_id


@pytest.mark.asyncio
@pytest.mark.parametrize(("revocation", "close_code"), [("membership", 4003), ("inactive", 4001), ("expired", 4003)])
async def test_open_loop_revocation_blocks_next_turn_before_persistence(monkeypatch, revocation, close_code):
    # Given an accepted socket whose first turn has a dynamic department grant.
    tenant = uuid4()
    user = UserRecord(id=uuid4(), tenant_id=tenant)
    user.identity = IdentityRecord(id=uuid4())
    agent = AgentRecord(id=uuid4(), name="Sales", creator_id=uuid4(), tenant_id=tenant, access_mode="custom")
    state = {"granted": True, "turn": 0, "connected": False}
    sent = []

    async def receive():
        if not state["connected"]:
            state["connected"] = True
            return {"type": "websocket.connect"}
        state["turn"] += 1
        if state["turn"] > 3:
            raise AssertionError("Revoked socket kept receiving turns")
        return {
            "type": "websocket.receive",
            "text": json.dumps({"content": "hello", "client_submission_id": str(uuid4())}),
        }

    async def send(message):
        sent.append(message)

    async def identity(_id, *, fresh=False):
        assert fresh
        return user

    async def department_access(_user, _agent):
        return state["granted"]

    async def dispatch(_prepared, **_kwargs):
        if revocation == "membership":
            state["granted"] = False
        elif revocation == "inactive":
            user.is_active = False
        else:
            agent.is_expired = True

    socket = WebSocket({"type": "websocket", "path": "/ws/chat", "headers": []}, receive, send)
    await socket.accept()
    handler = ws_api.WebSocketChatHandler(socket, agent.id, "token")
    handler.binding = chat_tickets.ChatTicketBinding(
        user_id=user.id,
        tenant_id=tenant,
        agent_id=agent.id,
        session_id=shared_session_id(agent.id),
        auth_deadline=int(time.time()) + 3600,
    )
    handler.conv_id = str(handler.binding.session_id)

    @asynccontextmanager
    async def turn_connection():
        yield

    monkeypatch.setattr(chat_submissions, "connection_ctx", turn_connection)
    monkeypatch.setattr(chat_submissions, "charge_conversation_quota", AsyncMock())
    monkeypatch.setattr(chat_submissions, "check_agent_expired", AsyncMock())
    monkeypatch.setattr(chat_submissions, "prepare_openclaw_message", AsyncMock())
    monkeypatch.setattr(chat_submissions, "enqueue_prepared_openclaw_message", dispatch)
    session = ChatSessionRecord(
        id=handler.binding.session_id, agent_id=agent.id, user_id=user.id, source_channel="shared"
    )
    monkeypatch.setattr(chat_submissions.chat_session_dao, "lock_for_agent", AsyncMock(return_value=session))
    monkeypatch.setattr(chat_submissions.chat_session_dao, "update", AsyncMock())
    monkeypatch.setattr(chat_submissions.chat_message_dao, "find_submission", AsyncMock(return_value=None))
    monkeypatch.setattr(chat_submissions.chat_message_dao, "list_recent", AsyncMock(return_value=[]))
    persist = AsyncMock(
        return_value=ChatMessageRecord(id=uuid4(), agent_id=agent.id, user_id=user.id, role="user", content="hello")
    )
    monkeypatch.setattr(chat_submissions.chat_message_dao, "reserve_submission", persist)
    monkeypatch.setattr(chat_tickets, "authorized_session", AsyncMock())
    monkeypatch.setattr(security.user_dao, "get_with_identity", identity)
    monkeypatch.setattr(permissions.user_dao, "get", AsyncMock(return_value=user))
    monkeypatch.setattr(permissions.agent_dao, "get", AsyncMock(return_value=agent))
    monkeypatch.setattr(permissions.agent_permission_dao, "list_for_agent", AsyncMock(return_value=[]))
    monkeypatch.setattr(permissions.org_department_dao, "has_agent_access", department_access)
    # A stale Redis allow must not override the fresh turn's database policy.
    monkeypatch.setattr(access_cache, "get_cached_level", AsyncMock(return_value="use"))
    monkeypatch.setattr(ws_api.manager, "send_to_session", AsyncMock())
    monkeypatch.setattr(handler, "_check_quotas", AsyncMock(return_value=True))
    # When the first dispatch revokes access and the next frame is received.
    await handler.message_loop()
    # Then only the first message persisted and the existing error/close envelope was sent.
    assert persist.await_count == 1
    assert sent[-1] == {"type": "websocket.close", "code": close_code, "reason": ""}
    assert '"type":"error"' in sent[-2]["text"]
