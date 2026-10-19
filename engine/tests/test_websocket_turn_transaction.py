import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import override
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from starlette.websockets import WebSocket, WebSocketDisconnect

from app.api import background_tasks, websocket as ws_api
from app.dao.user_dao import user_dao
from app.db import session
from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.records.user import UserRecord
from app.services import chat_tickets, openclaw_routing as routing
from app.services.quota_guard import AgentExpired
from test_db_session import _FakePool, _FakeRawConnection


@dataclass
class TurnState:
    pending: list[str] = field(default_factory=list)
    committed: list[str] = field(default_factory=list)
    frames: list[dict[str, object]] = field(default_factory=list)
    prepared_history: list[object] = field(default_factory=list)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    fail_enqueue: bool = False
    missing_model: bool = False
    already_onboarded: bool = False
    claim_won: bool = True
    charge_allowed: bool = True


class TurnConnection(_FakeRawConnection):
    def __init__(self, state: TurnState):
        super().__init__()
        self.state = state

    @override
    async def commit(self):
        await super().commit()
        self.state.committed.extend(self.state.pending)
        self.state.pending.clear()

    @override
    async def rollback(self):
        await super().rollback()
        self.state.pending.clear()


@pytest.fixture
async def turn(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[TurnState, TurnConnection]]:
    state = TurnState()
    raw = TurnConnection(state)
    token = session._conn_ctx.set(None)
    monkeypatch.setattr(session, "get_pool", lambda: _FakePool(raw))
    background_tasks.start_background_tasks()

    async def charge(_user_id):
        assert session.get_connection() is not None
        if not state.charge_allowed:
            return None
        state.pending.append("charge")
        return 1

    async def claim(_agent_id, _user_id):
        assert session.get_connection() is not None
        if state.claim_won:
            state.pending.append("claim")
        return state.claim_won

    async def persist(**kwargs):
        assert session.get_connection() is not None
        state.pending.append("title" if kwargs.get("skip_insert") else "chat")

    async def prepare(**kwargs):
        assert session.get_connection() is None
        state.prepared_history.extend(kwargs["history"])
        if state.missing_model:
            raise routing.NoCompanyModelError("No company model is available.")
        return routing.PreparedOpenClawMessage(
            agent=kwargs["agent"],
            content=kwargs["content"],
            selected_slot="primary",
            guest_model_ref=None,
            complexity=None,
            routing_reason="test",
        )

    async def create(*, obj_in):
        assert session.get_connection() is not None
        state.pending.append("gateway")
        message = GatewayMessageRecord(id=uuid4(), agent_id=obj_in["agent_id"], content=obj_in["content"])
        if state.fail_enqueue:
            agent = AgentRecord(id=message.agent_id, name="Agent", creator_id=uuid4())
            routing._schedule_inbox_wake(agent, message.content, message.id)
            raise RuntimeError("private database failure")
        return message

    async def wake(_agent, _content, _message_id):
        assert raw.commits == 1
        assert session.get_connection() is None
        state.wake.set()

    async def onboarded(_agent_id, _user_id):
        return state.already_onboarded

    monkeypatch.setattr(user_dao, "charge_message_quota", charge)
    monkeypatch.setattr(user_dao, "get", AsyncMock(return_value=UserRecord(id=uuid4(), quota_message_limit=0)))
    monkeypatch.setattr(ws_api, "try_begin_onboarding_greeting", claim)
    monkeypatch.setattr(ws_api, "is_onboarded", onboarded)
    monkeypatch.setattr(ws_api, "persist_chat_message", persist)
    monkeypatch.setattr(ws_api, "prepare_openclaw_message", prepare)
    monkeypatch.setattr(ws_api, "check_agent_expired", AsyncMock())
    monkeypatch.setattr(routing.gateway_message_dao, "list_pending", AsyncMock(return_value=[]))
    monkeypatch.setattr(routing.gateway_message_dao, "create", create)
    monkeypatch.setattr(routing, "_run_inbox_wake", wake)
    yield state, raw
    await background_tasks.drain_background_tasks(grace_seconds=0)
    if routing._wake_tasks:
        await asyncio.gather(*routing._wake_tasks)
    background_tasks.start_background_tasks()
    session._conn_ctx.reset(token)


async def drive_turn(
    monkeypatch: pytest.MonkeyPatch, state: TurnState, *, onboarding: bool = False, role: str = "member"
):
    tenant_id = uuid4()
    user = UserRecord(id=uuid4(), tenant_id=tenant_id, role=role)
    agent = AgentRecord(id=uuid4(), name="Agent", creator_id=user.id)
    received = 0

    async def receive():
        nonlocal received
        received += 1
        if received == 1:
            return {"type": "websocket.connect"}
        if received == 2:
            text = '{"kind":"onboarding_trigger"}' if onboarding else '{"content":"hello"}'
            return {"type": "websocket.receive", "text": text}
        raise WebSocketDisconnect()

    async def send(message):
        if message["type"] == "websocket.send":
            from app.core.json_types import json_loads_value

            payload = json_loads_value(message["text"])
            assert isinstance(payload, dict)
            if payload.get("type") == "status":
                assert session.get_connection() is None
                assert len(handler.conversation) == 2
            state.frames.append(payload)
        else:
            state.frames.append(message)

    socket = WebSocket({"type": "websocket", "path": "/ws/chat", "headers": []}, receive, send)
    await socket.accept()
    state.frames.clear()
    handler = ws_api.WebSocketChatHandler(socket, agent.id, "ticket")
    handler.user = UserRecord(id=user.id, role="member")
    handler.conv_id = str(uuid4())
    handler.binding = chat_tickets.ChatTicketBinding(
        user_id=user.id,
        tenant_id=tenant_id,
        agent_id=agent.id,
        session_id=uuid4(),
        auth_deadline=int(time.time()) + 3600,
    )
    handler.conversation = [{"role": "assistant", "content": "prior"}]
    monkeypatch.setattr(chat_tickets, "authorize", AsyncMock(return_value=(user, agent, "use")))
    monkeypatch.setattr(chat_tickets, "authorized_session", AsyncMock())
    with pytest.raises(WebSocketDisconnect):
        await handler.message_loop()
    return handler


@pytest.mark.parametrize("onboarding", [False, True])
async def test_accepted_turn_commits_before_append_status_and_wake(monkeypatch, turn, onboarding):
    state, raw = turn

    handler = await drive_turn(monkeypatch, state, onboarding=onboarding)
    await asyncio.wait_for(state.wake.wait(), timeout=1)

    assert state.committed == (["claim", "title", "gateway"] if onboarding else ["charge", "chat", "gateway"])
    assert raw.commits == 1
    assert raw.rollbacks == 0
    assert state.frames == [{"type": "status", "content": "thinking"}]
    assert len(handler.conversation) == 2
    assert state.prepared_history == [{"role": "assistant", "content": "prior"}]


@pytest.mark.parametrize("onboarding", [False, True])
@pytest.mark.parametrize("reason", ["no_company_model", "enqueue_failed"])
async def test_rejection_leaves_no_charge_chat_claim_gateway_wake_or_context(monkeypatch, turn, onboarding, reason):
    state, raw = turn
    state.missing_model = reason == "no_company_model"
    state.fail_enqueue = reason == "enqueue_failed"

    handler = await drive_turn(monkeypatch, state, onboarding=onboarding)
    await background_tasks.drain_background_tasks(grace_seconds=0)

    assert not state.pending
    assert not state.committed
    assert not state.wake.is_set()
    assert raw.commits == 0
    assert raw.rollbacks == int(reason == "enqueue_failed")
    assert len(handler.conversation) == 1
    assert len(state.frames) == 1
    assert state.frames[0]["type"] == "turn_rejected"
    assert state.frames[0]["reason"] == reason
    assert "private" not in str(state.frames[0]["content"])


async def test_quota_rejection_rolls_back_and_keeps_socket_open(monkeypatch, turn):
    state, raw = turn
    state.charge_allowed = False

    handler = await drive_turn(monkeypatch, state)

    assert not state.committed
    assert not state.pending
    assert not state.wake.is_set()
    assert raw.rollbacks == 1
    assert state.frames[0]["reason"] == "quota_exceeded"
    assert len(state.frames) == 1
    assert len(handler.conversation) == 1


async def test_expiry_rejection_precedes_preparation_and_transaction(monkeypatch, turn):
    state, raw = turn
    monkeypatch.setattr(ws_api, "check_agent_expired", AsyncMock(side_effect=AgentExpired("Agent")))

    handler = await drive_turn(monkeypatch, state)

    assert raw.commits == raw.rollbacks == 0
    assert not state.prepared_history
    assert not state.committed
    assert state.frames[0]["reason"] == "agent_expired"
    assert len(state.frames) == 1
    assert len(handler.conversation) == 1


@pytest.mark.parametrize("already_onboarded", [False, True])
async def test_onboarding_duplicate_never_charges_or_enqueues(monkeypatch, turn, already_onboarded):
    state, _raw = turn
    state.already_onboarded = already_onboarded
    state.claim_won = False

    handler = await drive_turn(monkeypatch, state, onboarding=True)

    assert not state.committed
    assert not state.pending
    assert not state.wake.is_set()
    assert state.frames == [{"type": "onboarded", "agent_id": str(handler.agent_id)}]
    assert len(handler.conversation) == 1


@pytest.mark.parametrize("role", ["platform_admin", "org_admin"])
async def test_exemption_uses_reauthorized_principal_not_previous_handler_user(monkeypatch, turn, role):
    state, _raw = turn
    state.charge_allowed = False

    await drive_turn(monkeypatch, state, role=role)

    assert state.committed == ["chat", "gateway"]
    assert state.frames == [{"type": "status", "content": "thinking"}]
