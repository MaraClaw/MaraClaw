from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from starlette.websockets import WebSocket

from app.api import websocket as ws_api
from app.core import access_cache, permissions, security
from app.records.agent import AgentRecord
from app.records.user import UserRecord


@pytest.mark.asyncio
@pytest.mark.parametrize(("revocation", "close_code"), [("membership", 4003), ("inactive", 4001), ("expired", 4003)])
async def test_open_loop_revocation_blocks_next_turn_before_persistence(monkeypatch, revocation, close_code):
    # Given an accepted socket whose first turn has a dynamic department grant.
    tenant = uuid4()
    user = UserRecord(id=uuid4(), tenant_id=tenant)
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
        return {"type": "websocket.receive", "text": '{"content":"hello"}'}

    async def send(message):
        sent.append(message)

    async def identity(_id, *, fresh=False):
        assert fresh
        return user

    async def department_access(_user, _agent):
        return state["granted"]

    async def dispatch(_content):
        if revocation == "membership":
            state["granted"] = False
        elif revocation == "inactive":
            user.is_active = False
        else:
            agent.is_expired = True

    socket = WebSocket({"type": "websocket", "path": "/ws/chat", "headers": []}, receive, send)
    await socket.accept()
    handler = ws_api.WebSocketChatHandler(socket, agent.id, "token")
    monkeypatch.setattr(security, "decode_access_token", lambda _token: {"sub": str(user.id)})
    monkeypatch.setattr(security.user_dao, "get_with_identity", identity)
    monkeypatch.setattr(permissions.user_dao, "get", AsyncMock(return_value=user))
    monkeypatch.setattr(permissions.agent_dao, "get", AsyncMock(return_value=agent))
    monkeypatch.setattr(permissions.agent_permission_dao, "list_for_agent", AsyncMock(return_value=[]))
    monkeypatch.setattr(permissions.local_department_dao, "has_agent_access", department_access)
    # A stale Redis allow must not override the fresh turn's database policy.
    monkeypatch.setattr(access_cache, "get_cached_level", AsyncMock(return_value="use"))
    monkeypatch.setattr(handler, "_check_quotas", AsyncMock(return_value=True))
    persist = AsyncMock()
    monkeypatch.setattr(handler, "_save_user_message", persist)
    monkeypatch.setattr(handler, "_route_openclaw", dispatch)
    # When the first dispatch revokes access and the next frame is received.
    await handler.message_loop()
    # Then only the first message persisted and the existing error/close envelope was sent.
    assert persist.await_count == 1
    assert sent[-1] == {"type": "websocket.close", "code": close_code, "reason": ""}
    assert '"type":"error"' in sent[-2]["text"]
