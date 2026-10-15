from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.llm import caller
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
@pytest.mark.parametrize("existing", [False, True], ids=["create", "promote"])
@pytest.mark.parametrize("guarded", [False, True], ids=["default", "expired"])
async def test_primary_session_checks_guard_after_read_before_mutation(monkeypatch, existing, guarded):
    from app.services import chat_session_service as service

    # Given a read-before-write session selection with no primary session.
    row = SimpleNamespace(id=uuid4())
    writes = []
    monkeypatch.setattr(service, "get_primary_platform_session", lambda *_a: _async_return(None))
    monkeypatch.setattr(
        service.chat_session_dao, "find_best_web_session", lambda **_k: _async_return(row if existing else None)
    )

    async def mutate(**_kwargs):
        writes.append(True)
        return row

    monkeypatch.setattr(service.chat_session_dao, "create", mutate)
    monkeypatch.setattr(service.chat_session_dao, "update", mutate)
    # When admission is checked after the lookup.
    if guarded:
        with pytest.raises(ExecutionLeaseLostError):
            await service.ensure_primary_platform_session(uuid4(), uuid4(), guard=lost)
    else:
        assert await service.ensure_primary_platform_session(uuid4(), uuid4()) is row
    # Then only the default, unguarded call mutates the session.
    assert writes == ([] if guarded else [True])


@pytest.mark.asyncio
@pytest.mark.parametrize("guarded", [False, True], ids=["default", "expired"])
async def test_read_mark_checks_guard_after_lookup_before_update(monkeypatch, guarded):
    from app.api import websocket

    # Given an active viewer and an existing session.
    writes = []
    monkeypatch.setattr(websocket.manager, "is_user_viewing_session", lambda *_a: _async_return(True))
    monkeypatch.setattr(websocket.chat_session_dao, "get", lambda *_a: _async_return(SimpleNamespace(id=uuid4())))

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
