import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from tests.test_background_llm import background_case, tool_response

from app.dao.agent_dao import agent_dao
from app.records.chat import ChatSessionRecord
from app.services.llm import caller
from app.services.llm.background_guard import ScheduleLeaseLostError
from app.services.llm.types import LLMResponse
from app.services.trigger_runtime.executions import ExecutionLeaseLostError
from test_finish_protocol import FakeStreamClient, _async_return, _model


def lost():
    raise ExecutionLeaseLostError("expired")


@pytest.mark.asyncio
async def test_guard_stops_second_model_round_without_becoming_error_string(monkeypatch):
    # Given a provider that requests another round and a lease lost between rounds.
    client = FakeStreamClient([LLMResponse(content="continue", tool_calls=[])])
    executed = []

    def guard():
        if client.messages_seen:
            lost()

    async def fresh(model, **_kwargs):
        return model, client

    async def execute(*_args, **_kwargs):
        executed.append(True)
        return "result"

    monkeypatch.setattr(caller, "_get_agent_config", lambda *_a, **_k: _async_return((3, None)))
    monkeypatch.setattr(agent_dao, "consume_llm_call", AsyncMock(return_value=object()))
    monkeypatch.setattr(caller, "_get_user_name", lambda *_a: _async_return(None))
    monkeypatch.setattr("app.services.agent_context.build_agent_context", lambda *_a, **_k: _async_return(("", "")))
    monkeypatch.setattr(caller, "create_fresh_llm_client_from_model", fresh)
    monkeypatch.setattr(caller, "execute_tool", execute)
    # When the second model round is admitted.
    with pytest.raises(ExecutionLeaseLostError, match="expired"):
        await caller.call_llm(_model(), [], "agent", "", uuid4(), skip_tools=True, side_effect_guard=guard)
    # Then only the first provider request ran and the lease error propagated.
    assert len(client.messages_seen) == 1
    assert executed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_at", [1, 2, 3], ids=["running", "execute", "done"])
async def test_tool_guard_stops_each_side_effect_phase(monkeypatch, stop_at):
    # Given sequential admission checks around persistence and execution.
    events = []
    checks = 0

    def guard():
        nonlocal checks
        checks += 1
        if checks == stop_at:
            lost()

    async def callback(data):
        events.append(data["status"])

    async def execute(*_args, **_kwargs):
        events.append("execute")
        return "result"

    monkeypatch.setattr(caller, "execute_tool", execute)
    # When a phase loses admission.
    with pytest.raises(ExecutionLeaseLostError, match="expired"):
        await caller._process_tool_call(
            tc={"id": "call", "function": {"name": "dummy", "arguments": "{}"}},
            api_messages=[],
            agent_id=uuid4(),
            user_id=None,
            session_id="session",
            supports_vision=False,
            on_tool_call=callback,
            full_reasoning_content="",
            allowed_tool_names=set(),
            side_effect_guard=guard,
        )
    # Then no callback or tool starts at or after the denied phase.
    assert events == ["running", "execute"][: stop_at - 1]


@pytest.mark.asyncio
async def test_lease_loss_raised_by_done_callback_is_not_swallowed(monkeypatch):
    # Given a done callback that finds the lease lost after the caller's check passed.
    expired = False

    def guard():
        if expired:
            lost()

    async def callback(data):
        nonlocal expired
        if data["status"] == "done":
            expired = True
            lost()

    async def execute(*_args, **_kwargs):
        return "result"

    monkeypatch.setattr(caller, "execute_tool", execute)
    # When the callback raises, then the lease loss propagates instead of being logged as a callback failure.
    with pytest.raises(ExecutionLeaseLostError, match="expired"):
        await caller._process_tool_call(
            tc={"id": "call", "function": {"name": "dummy", "arguments": "{}"}},
            api_messages=[],
            agent_id=uuid4(),
            user_id=None,
            session_id="session",
            supports_vision=False,
            on_tool_call=callback,
            full_reasoning_content="",
            allowed_tool_names=set(),
            side_effect_guard=guard,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["agent_read", "connection_checkout"])
@pytest.mark.parametrize("guarded", [False, True], ids=["default", "expired"])
async def test_primary_session_checks_guard_after_read_before_mutation(monkeypatch, phase, guarded):
    from app.dao import shared_chat_dao
    from app.services import chat_session_service as service, shared_chat

    agent_id, user_id = uuid4(), uuid4()
    row = ChatSessionRecord(
        id=shared_chat.shared_session_id(agent_id), agent_id=agent_id, user_id=user_id, source_channel="shared"
    )
    connection = SimpleNamespace(execute=AsyncMock(), fetchone=AsyncMock(return_value=asdict(row)))
    expired = False

    async def get_agent(_agent_id):
        nonlocal expired
        expired = phase == "agent_read"
        return SimpleNamespace(creator_id=user_id)

    @asynccontextmanager
    async def checkout():
        nonlocal expired
        expired = True
        yield connection

    def guard():
        if expired:
            lost()

    monkeypatch.setattr(shared_chat.agent_dao, "get", get_agent)
    monkeypatch.setattr(shared_chat_dao, "connection_ctx", checkout)
    if guarded:
        with pytest.raises(ExecutionLeaseLostError):
            await service.ensure_primary_platform_session(agent_id, user_id, guard=guard)
    else:
        assert await service.ensure_primary_platform_session(agent_id, user_id) == row
    assert connection.execute.await_count == (0 if guarded else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["provider", "tool"])
@pytest.mark.parametrize("fails", [False, True], ids=["returns", "raises"])
async def test_background_revocation_during_await_stops_effects(monkeypatch, phase, fails):
    state = background_case(monkeypatch, [tool_response(2)], fallback=True)
    entered, release = asyncio.Event(), asyncio.Event()
    if phase == "provider":
        state.client.paused = True
        state.client.pause_at = 2
        entered, release = state.client.entered, state.client.release
        state.client.responses = [
            LLMResponse(content="continue", usage={"total_tokens": 17}),
            RuntimeError("503 unavailable") if fails else tool_response(2),
        ]
    else:

        async def execute(*_args, **_kwargs):
            state.executed.append("dummy")
            entered.set()
            await release.wait()
            if fails:
                raise RuntimeError("503 tool failed")
            return "result"

        monkeypatch.setattr(caller, "execute_tool", execute)

    def classify(_error):
        pytest.fail("Ownership loss must not reach provider classification")

    monkeypatch.setattr(caller, "classify_error", classify)
    task = asyncio.create_task(
        caller.call_agent_llm_with_tools(state.agent.id, "system", "user", turn=state.turn, guard=state.guard)
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    state.guard.database_held = False
    assert state.guard.held
    release.set()
    with pytest.raises(ScheduleLeaseLostError, match="expired"):
        await task
    assert state.executed == (["dummy"] if phase == "tool" else [])
    assert state.usage == []
    assert state.created == ["fake-model"]
    assert len(state.client.messages_seen) == (2 if phase == "provider" else 1)
    assert state.client.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("guarded", [False, True], ids=["default", "expired"])
async def test_read_mark_checks_guard_after_lookup_before_update(monkeypatch, guarded):
    from app.api import websocket

    # Given an active viewer and an existing session.
    writes = []
    monkeypatch.setattr(websocket.manager, "is_user_viewing_session", lambda *_a: _async_return(True))
    session = ChatSessionRecord(id=uuid4(), agent_id=uuid4(), user_id=uuid4())
    monkeypatch.setattr(websocket.chat_session_dao, "get", lambda *_a: _async_return(session))

    async def update(**_kwargs):
        writes.append(True)

    monkeypatch.setattr(websocket.chat_session_dao, "update", update)
    agent_id, session_id, user_id = uuid4(), str(uuid4()), uuid4()
    # When the helper is asked to advance the read mark.
    if guarded:
        with pytest.raises(ExecutionLeaseLostError):
            await websocket.maybe_mark_session_read_for_active_viewer(
                agent_id=agent_id, session_id=session_id, user_id=user_id, guard=lost
            )
    else:
        assert await websocket.maybe_mark_session_read_for_active_viewer(
            agent_id=agent_id, session_id=session_id, user_id=user_id
        )
    # Then a denied write never starts; the default behavior remains unchanged.
    assert writes == ([] if guarded else [True])
