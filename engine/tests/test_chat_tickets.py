import asyncio
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anyio
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from redis.exceptions import RedisError

from app.api import websocket
from app.services import chat_tickets as tickets


class TicketRedis:
    def __init__(self):
        self.values = {}
        self.now = 0

    async def set(self, key, value, *, nx, ex):
        assert nx
        assert ex == 60
        if key in self.values:
            return False
        self.values[key] = (value, self.now + ex)
        return True

    async def getdel(self, key):
        value = self.values.pop(key, None)
        return value[0] if value and value[1] > self.now else None


@pytest.fixture
def context(monkeypatch):
    redis = TicketRedis()
    binding = tickets.ChatTicketBinding(
        user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        auth_deadline=int(time.time()) + 3600,
    )
    user = SimpleNamespace(
        id=binding.user_id, tenant_id=binding.tenant_id, identity=SimpleNamespace(is_active=True), display_name="Member"
    )
    agent = SimpleNamespace(
        id=binding.agent_id,
        name="Agent",
        agent_type="",
        role_description="",
        welcome_message="",
        context_window_size=100,
        is_expired=False,
        expires_at=None,
    )
    monkeypatch.setattr(tickets, "get_redis", AsyncMock(return_value=redis))
    monkeypatch.setattr(tickets, "load_user_principal", AsyncMock(return_value=user))
    monkeypatch.setattr(tickets, "check_agent_access", AsyncMock(return_value=(agent, "use")))
    monkeypatch.setattr(
        tickets.chat_session_dao,
        "get_for_agent",
        AsyncMock(return_value=SimpleNamespace(user_id=binding.user_id, source_channel="web")),
    )
    monkeypatch.setattr(websocket.manager, "connect", AsyncMock())
    monkeypatch.setattr(websocket.manager, "disconnect", AsyncMock())
    monkeypatch.setattr(websocket.WebSocketChatHandler, "_load_history", AsyncMock())
    return redis, binding, user, agent


async def test_ticket_race_has_one_winner_and_no_raw_key(context):
    redis, binding, _, _ = context
    issued = await tickets.issue(binding)
    assert issued.ticket not in next(iter(redis.values))
    results = await asyncio.gather(*(tickets.redeem(issued.ticket) for _ in range(20)), return_exceptions=True)
    assert sum(result == binding for result in results) == 1
    assert sum(isinstance(result, HTTPException) for result in results) == 19


async def test_ticket_ttl_does_not_change_auth_deadline(context):
    redis, binding, _, _ = context
    issued = await tickets.issue(binding)
    redis.now = 59
    assert (await tickets.redeem(issued.ticket)).auth_deadline == binding.auth_deadline
    issued = await tickets.issue(binding)
    redis.now += 60
    with pytest.raises(HTTPException) as error:
        await tickets.redeem(issued.ticket)
    assert error.value.status_code == 400


@pytest.mark.parametrize("operation", ["issue", "redeem"])
async def test_redis_failure_is_closed(context, monkeypatch, operation):
    _, binding, _, _ = context
    monkeypatch.setattr(tickets, "get_redis", AsyncMock(side_effect=RedisError()))
    with pytest.raises(HTTPException) as error:
        await (tickets.issue(binding) if operation == "issue" else tickets.redeem("opaque"))
    assert error.value.status_code == 503


@pytest.mark.parametrize(("change", "code"), [("identity", 401), ("tenant", 403), ("expired", 403), ("deadline", 401)])
async def test_fresh_policy_is_checked(context, change, code):
    _, binding, user, agent = context
    if change == "identity":
        user.identity = None
    elif change == "tenant":
        user.tenant_id = uuid.uuid4()
    elif change == "expired":
        agent.is_expired = True
    else:
        binding.auth_deadline = 0
    with pytest.raises(HTTPException) as error:
        await tickets.issue(binding)
    assert error.value.status_code == code


@pytest.mark.parametrize(("source", "allowed"), [("web", False), ("agent", True)])
async def test_session_sharing_is_preserved(context, monkeypatch, source, allowed):
    _, binding, _, _ = context
    monkeypatch.setattr(
        tickets.chat_session_dao,
        "get_for_agent",
        AsyncMock(return_value=SimpleNamespace(user_id=uuid.uuid4(), source_channel=source)),
    )
    if allowed:
        await tickets.authorized_session(binding)
    else:
        with pytest.raises(HTTPException):
            await tickets.authorized_session(binding)


async def test_socket_mismatch_consumes_ticket_without_fallback(context):
    _, binding, _, _ = context
    issued = await tickets.issue(binding)
    app = FastAPI()
    app.include_router(websocket.router)
    with TestClient(app) as client:
        with client.websocket_connect(
            f"/ws/chat/{binding.agent_id}?ticket={issued.ticket}&session_id={uuid.uuid4()}"
        ) as ws:
            assert ws.receive_json()["type"] == "error"
            assert ws.receive()["code"] == 4002
        with client.websocket_connect(
            f"/ws/chat/{binding.agent_id}?ticket={issued.ticket}&session_id={binding.session_id}"
        ) as ws:
            assert ws.receive_json()["type"] == "error"
            assert ws.receive()["code"] == 4002


async def test_socket_idle_absolute_deadline_and_cleanup(context, monkeypatch):
    _, binding, _, _ = context
    disconnect = AsyncMock()
    monkeypatch.setattr(websocket.manager, "disconnect", disconnect)
    issued = await tickets.issue(binding)
    monkeypatch.setattr(websocket.time, "time", lambda: binding.auth_deadline - 0.02)
    app = FastAPI()
    app.include_router(websocket.router)
    with (
        TestClient(app) as client,
        client.websocket_connect(
            f"/ws/chat/{binding.agent_id}?ticket={issued.ticket}&session_id={binding.session_id}"
        ) as ws,
    ):
        assert ws.receive_json() == {"type": "connected", "session_id": str(binding.session_id)}
        assert ws.receive()["code"] == 4001
    disconnect.assert_awaited_once()


@pytest.mark.parametrize("phase", ["setup", "work"])
async def test_socket_deadline_cancels_inflight_phase(context, monkeypatch, phase):
    _, binding, _, _ = context
    issued = await tickets.issue(binding)
    cancelled = []

    async def blocked(*args):
        try:
            await anyio.sleep_forever()
        finally:
            cancelled.append(True)

    method = "_load_history" if phase == "setup" else "_route_openclaw"
    monkeypatch.setattr(websocket.WebSocketChatHandler, method, blocked)
    monkeypatch.setattr(websocket.WebSocketChatHandler, "_save_user_message", AsyncMock())
    monkeypatch.setattr(websocket.WebSocketChatHandler, "_check_quotas", AsyncMock(return_value=True))
    monkeypatch.setattr(websocket.time, "time", lambda: binding.auth_deadline - 0.05)
    app = FastAPI()
    app.include_router(websocket.router)
    with (
        TestClient(app) as client,
        client.websocket_connect(
            f"/ws/chat/{binding.agent_id}?ticket={issued.ticket}&session_id={binding.session_id}"
        ) as ws,
    ):
        if phase == "work":
            assert ws.receive_json()["type"] == "connected"
            ws.send_json({"content": "hello"})
        assert ws.receive()["code"] == 4001
    assert cancelled == [True]


@pytest.mark.parametrize("query", ["", "token=legacy-jwt"])
def test_socket_never_accepts_legacy_jwt(query):
    app = FastAPI()
    app.include_router(websocket.router)
    with TestClient(app) as client, client.websocket_connect(f"/ws/chat/{uuid.uuid4()}?{query}") as ws:
        assert ws.receive_json()["type"] == "error"
        assert ws.receive()["code"] == 4002


async def test_deleted_session_is_not_replaced(context, monkeypatch):
    _, binding, _, _ = context
    monkeypatch.setattr(tickets.chat_session_dao, "get_for_agent", AsyncMock(return_value=None))
    creator = AsyncMock()
    monkeypatch.setattr(tickets, "ensure_primary_platform_session", creator)
    with pytest.raises(HTTPException) as error:
        await tickets.authorized_session(binding)
    assert error.value.status_code == 403
    creator.assert_not_awaited()


@pytest.mark.parametrize(("status", "close_code"), [(401, 4001), (403, 4003), (503, 1011)])
async def test_socket_redemption_policy_and_redis_failure(context, monkeypatch, status, close_code):
    _, binding, _, _ = context
    issued = await tickets.issue(binding)
    if status == 503:
        monkeypatch.setattr(tickets, "get_redis", AsyncMock(side_effect=RedisError()))
    else:
        monkeypatch.setattr(tickets, "load_user_principal", AsyncMock(side_effect=HTTPException(status, "denied")))
    app = FastAPI()
    app.include_router(websocket.router)
    with (
        TestClient(app) as client,
        client.websocket_connect(
            f"/ws/chat/{binding.agent_id}?ticket={issued.ticket}&session_id={binding.session_id}"
        ) as ws,
    ):
        assert ws.receive_json()["type"] == "error"
        assert ws.receive()["code"] == close_code
    if status != 503:
        with pytest.raises(HTTPException) as error:
            await tickets.redeem(issued.ticket)
        assert error.value.status_code == 400


async def test_failed_setup_does_not_restore_ticket(context, monkeypatch):
    _, binding, _, _ = context
    issued = await tickets.issue(binding)
    monkeypatch.setattr(websocket.manager, "connect", AsyncMock(side_effect=RuntimeError("setup failure")))
    app = FastAPI()
    app.include_router(websocket.router)
    with (
        TestClient(app) as client,
        client.websocket_connect(
            f"/ws/chat/{binding.agent_id}?ticket={issued.ticket}&session_id={binding.session_id}"
        ) as ws,
    ):
        assert ws.receive()["code"] == 1011
    with pytest.raises(HTTPException) as error:
        await tickets.redeem(issued.ticket)
    assert error.value.status_code == 400
