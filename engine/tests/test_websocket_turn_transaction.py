import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import override
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from starlette.websockets import WebSocket, WebSocketDisconnect

from app.api import background_tasks, websocket as ws_api
from app.dao.user_dao import user_dao
from app.db import session
from app.records.agent import AgentRecord
from app.records.chat import ChatMessageRecord, ChatSessionRecord
from app.records.gateway_message import GatewayMessageRecord
from app.records.llm import LLMModelRecord
from app.records.user import UserRecord
from app.services import (
    chat_submissions,
    chat_tickets,
    openclaw_routing as routing,
    openclaw_update_exclusion as exclusion,
)
from app.services.agent_setup_lock import setup_workspace_lock
from app.services.llm.turn import ModelBundle
from app.services.openclaw_update_gate import OpenClawUpdateInProgressError, OpenClawUpdateRequiredError
from app.services.quota_guard import AgentExpired
from app.services.shared_chat import shared_session_id
from test_db_session import _FakePool, _FakeRawConnection

NOTICE_ID = uuid4()


@dataclass
class TurnState:
    pending: list[str] = field(default_factory=list)
    committed: list[str] = field(default_factory=list)
    frames: list[dict[str, object]] = field(default_factory=list)
    prepared_history: list[object] = field(default_factory=list)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    fail_enqueue: bool = False
    missing_model: bool = False
    gate: Exception | None = None
    already_onboarded: bool = False
    claim_won: bool = True
    charge_allowed: bool = True
    pending_messages: list[ChatMessageRecord] = field(default_factory=list)
    messages: list[ChatMessageRecord] = field(default_factory=list)
    user: UserRecord = field(default_factory=lambda: UserRecord(id=uuid4(), tenant_id=uuid4()))
    agent: AgentRecord = field(default_factory=lambda: AgentRecord(id=uuid4(), name="Agent", creator_id=uuid4()))
    session_id: str = field(default_factory=lambda: str(uuid4()))
    submission_id: str = field(default_factory=lambda: str(uuid4()))
    session_exists: bool = True
    lookup_count: int = 0


class TurnConnection(_FakeRawConnection):
    def __init__(self, state: TurnState):
        super().__init__()
        self.state = state

    @override
    async def commit(self):
        await super().commit()
        self.state.committed.extend(self.state.pending)
        self.state.pending.clear()
        self.state.messages.extend(self.state.pending_messages)
        self.state.pending_messages.clear()

    @override
    async def rollback(self):
        await super().rollback()
        self.state.pending.clear()
        self.state.pending_messages.clear()


@pytest.fixture
async def turn(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[TurnState, TurnConnection]]:
    state = TurnState()
    state.session_id = str(shared_session_id(state.agent.id))
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
        assert (session.get_connection() is None) == (kwargs["content"] == "Please begin the onboarding.")
        state.prepared_history.extend(kwargs["history"])
        if state.gate is not None:
            raise state.gate
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
        assert raw.commits >= 1
        assert session.get_connection() is None
        state.wake.set()

    async def onboarded(_agent_id, _user_id):
        return state.already_onboarded

    async def lock(session_id, agent_id):
        assert session.get_connection() is not None
        if not state.session_exists:
            return None
        return ChatSessionRecord(
            id=session_id, agent_id=agent_id, user_id=state.agent.creator_id, source_channel="shared"
        )

    async def get_session(session_id, agent_id):
        return ChatSessionRecord(
            id=session_id, agent_id=agent_id, user_id=state.agent.creator_id, source_channel="shared"
        )

    async def recent(**_kwargs):
        return [
            ChatMessageRecord(
                id=uuid4(), agent_id=state.agent.id, user_id=state.user.id, role="assistant", content="prior"
            )
        ]

    async def lookup(candidate):
        assert session.get_connection() is not None
        state.lookup_count += 1
        return next(
            (
                message
                for message in state.messages
                if (message.user_id, message.agent_id, message.conversation_id, message.client_submission_id)
                == (candidate.user_id, candidate.agent_id, candidate.conversation_id, candidate.client_submission_id)
            ),
            None,
        )

    async def reserve(candidate):
        assert session.get_connection() is not None
        state.pending.append("chat")
        state.pending_messages.append(candidate)
        return candidate

    monkeypatch.setattr(chat_submissions.chat_session_dao, "lock_for_agent", lock)
    monkeypatch.setattr(chat_submissions.chat_session_dao, "get_for_agent", get_session)
    monkeypatch.setattr(chat_submissions.chat_message_dao, "list_recent", recent)
    monkeypatch.setattr(ws_api.manager, "send_to_session", AsyncMock())
    monkeypatch.setattr(chat_submissions.chat_session_dao, "update", AsyncMock())
    monkeypatch.setattr(chat_submissions.chat_message_dao, "find_submission", lookup)
    monkeypatch.setattr(chat_submissions.chat_message_dao, "reserve_submission", reserve)
    monkeypatch.setattr(chat_submissions, "prepare_openclaw_message", prepare)
    monkeypatch.setattr(chat_submissions, "check_agent_expired", AsyncMock())
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
    monkeypatch: pytest.MonkeyPatch,
    state: TurnState,
    *,
    onboarding: bool = False,
    role: str = "member",
    payload: dict[str, object] | None = None,
    denied: bool = False,
):
    user, agent = state.user, state.agent
    user.role = role
    received = 0

    async def receive():
        nonlocal received
        received += 1
        if received == 1:
            return {"type": "websocket.connect"}
        if received == 2:
            data = payload if payload is not None else {"content": "hello", "client_submission_id": state.submission_id}
            text = '{"kind":"onboarding_trigger"}' if onboarding else json.dumps(data)
            return {"type": "websocket.receive", "text": text}
        raise WebSocketDisconnect()

    async def send(message):
        if message["type"] == "websocket.send":
            from app.core.json_types import json_loads_value

            payload = json_loads_value(message["text"])
            assert isinstance(payload, dict)
            if payload.get("type") in ("status", "turn_accepted"):
                assert session.get_connection() is None
                assert state.committed
                assert not state.pending
            if payload.get("type") == "status":
                assert len(handler.conversation) == 2
            state.frames.append(payload)
        else:
            state.frames.append(message)

    socket = WebSocket({"type": "websocket", "path": "/ws/chat", "headers": []}, receive, send)
    await socket.accept()
    state.frames.clear()
    handler = ws_api.WebSocketChatHandler(socket, agent.id, "ticket")
    handler.user = UserRecord(id=user.id, role="member")
    handler.conv_id = state.session_id
    assert user.tenant_id is not None
    handler.binding = chat_tickets.ChatTicketBinding(
        user_id=user.id,
        tenant_id=user.tenant_id,
        agent_id=agent.id,
        session_id=UUID(state.session_id),
        auth_deadline=int(time.time()) + 3600,
        auth_generation=0,
    )
    handler.conversation = [{"role": "assistant", "content": "prior"}]
    from fastapi import HTTPException

    monkeypatch.setattr(
        chat_tickets,
        "authorize",
        AsyncMock(side_effect=HTTPException(403, "Revoked"))
        if denied
        else AsyncMock(return_value=(user, agent, "use")),
    )
    monkeypatch.setattr(chat_tickets, "authorized_session", AsyncMock())
    if denied or not state.session_exists:
        await handler.message_loop()
    else:
        with pytest.raises(WebSocketDisconnect):
            await handler.message_loop()
    return handler


@pytest.mark.parametrize("onboarding", [False, True])
async def test_accepted_turn_commits_before_append_status_and_wake(monkeypatch, turn, onboarding):
    state, raw = turn

    handler = await drive_turn(monkeypatch, state, onboarding=onboarding)
    await asyncio.wait_for(state.wake.wait(), timeout=1)

    assert state.committed == (["claim", "title", "gateway"] if onboarding else ["chat", "charge", "gateway"])
    assert raw.commits == 1
    assert raw.rollbacks == 0
    expected = [{"type": "status", "content": "thinking"}]
    if not onboarding:
        expected.insert(
            0,
            {
                "type": "turn_accepted",
                "message_id": str(state.messages[0].id),
                "session_id": handler.conv_id,
                "client_submission_id": state.submission_id,
            },
        )
    assert state.frames == expected
    assert len(handler.conversation) == 2
    assert state.prepared_history == [{"role": "assistant", "content": "prior"}]


@pytest.mark.parametrize("onboarding", [False, True])
@pytest.mark.parametrize("reason", ["no_company_model", "enqueue_failed", "guest_config_failed"])
async def test_rejection_leaves_no_charge_chat_claim_gateway_wake_or_context(monkeypatch, turn, onboarding, reason):
    state, raw = turn
    state.missing_model = reason == "no_company_model"
    state.fail_enqueue = reason == "enqueue_failed"
    if reason == "guest_config_failed":
        state.gate = routing.GuestConfigWriteError("private config details")

    handler = await drive_turn(monkeypatch, state, onboarding=onboarding)
    await background_tasks.drain_background_tasks(grace_seconds=0)

    assert not state.pending
    assert not state.committed
    assert not state.wake.is_set()
    assert raw.commits == 0
    assert raw.rollbacks == int(not onboarding or reason == "enqueue_failed")
    assert len(handler.conversation) == 1
    assert len(state.frames) == 1
    assert state.frames[0]["type"] == "turn_rejected"
    assert state.frames[0]["reason"] == reason
    assert "private" not in str(state.frames[0]["content"])
    assert not state.messages
    if not onboarding:
        assert state.frames[0]["client_submission_id"] == state.submission_id


@pytest.mark.parametrize("onboarding", [False, True])
@pytest.mark.parametrize("failure", ["write", "timeout", "upgrade"])
async def test_real_config_preparation_rejects_without_accepting_a_turn(
    monkeypatch: pytest.MonkeyPatch, turn, tmp_path: Path, onboarding: bool, failure: str
):
    # Given real preparation with a broken writer, contended fence, or locked gate recheck.
    state, raw = turn
    model = LLMModelRecord(uuid4(), "anthropic", "opus", "encrypted-key", "Primary")
    monkeypatch.setattr(chat_submissions, "prepare_openclaw_message", routing.prepare_openclaw_message)
    monkeypatch.setattr(ws_api, "prepare_openclaw_message", routing.prepare_openclaw_message)
    monkeypatch.setattr(routing, "assert_work_allowed", AsyncMock())
    monkeypatch.setattr(routing, "ensure_agent_company_models", AsyncMock(return_value=state.agent))
    monkeypatch.setattr(routing, "get_cached_bundle", lambda _agent: ModelBundle(primary=model))
    gate = AsyncMock(side_effect=[None, OpenClawUpdateInProgressError()]) if failure == "upgrade" else AsyncMock()
    monkeypatch.setattr(exclusion, "assert_no_active_run", gate)
    monkeypatch.setattr(exclusion, "assert_transfer_ready", AsyncMock())
    monkeypatch.setattr(exclusion.agent_manager, "_agent_dir", lambda agent_id: tmp_path / str(agent_id))
    monkeypatch.setattr(exclusion, "GUEST_WRITE_LOCK_TIMEOUT_SECONDS", 0)
    writer = Mock(side_effect=OSError("private config path"))
    monkeypatch.setattr(routing.agent_manager, "write_guest_config", writer)
    # When driving the actual WebSocket turn through preparation.
    if failure == "timeout":
        async with setup_workspace_lock(tmp_path / str(state.agent.id)):
            handler = await drive_turn(monkeypatch, state, onboarding=onboarding)
    else:
        handler = await drive_turn(monkeypatch, state, onboarding=onboarding)
    # Then neither success frames nor durable or in-memory acceptance survive.
    reason = "openclaw_update_in_progress" if failure == "upgrade" else "guest_config_failed"
    assert len(state.frames) == 1
    assert state.frames[0]["type"] == "turn_rejected"
    assert state.frames[0]["reason"] == reason
    assert "private" not in str(state.frames[0]["content"])
    assert not state.pending
    assert not state.committed
    assert not state.messages
    assert not state.wake.is_set()
    assert raw.commits == 0
    assert raw.rollbacks == int(not onboarding)
    assert len(handler.conversation) == 1
    if failure != "write":
        writer.assert_not_called()


@pytest.mark.parametrize("onboarding", [False, True])
@pytest.mark.parametrize(
    ("gate", "extra"),
    [
        (OpenClawUpdateRequiredError(NOTICE_ID), {"notice_id": str(NOTICE_ID)}),
        (OpenClawUpdateInProgressError(), {}),
    ],
)
async def test_openclaw_update_gate_rejects_the_turn_without_side_effects(monkeypatch, turn, onboarding, gate, extra):
    state, raw = turn
    state.gate = gate

    handler = await drive_turn(monkeypatch, state, onboarding=onboarding)
    await background_tasks.drain_background_tasks(grace_seconds=0)

    assert not state.pending
    assert not state.committed
    assert not state.wake.is_set()
    assert raw.commits == 0
    assert raw.rollbacks == int(not onboarding)
    assert len(handler.conversation) == 1
    assert len(state.frames) == 1
    assert state.frames[0]["type"] == "turn_rejected"
    assert state.frames[0]["reason"] == gate.code
    assert state.frames[0].get("notice_id") == extra.get("notice_id")
    assert not state.messages
    if not onboarding:
        assert state.frames[0]["client_submission_id"] == state.submission_id


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


async def test_expiry_rejection_precedes_preparation_and_reservation(monkeypatch, turn):
    state, raw = turn
    monkeypatch.setattr(chat_submissions, "check_agent_expired", AsyncMock(side_effect=AgentExpired("Agent")))

    handler = await drive_turn(monkeypatch, state)

    assert raw.commits == 0
    assert raw.rollbacks == 1
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
    assert [frame["type"] for frame in state.frames] == ["turn_accepted", "status"]
