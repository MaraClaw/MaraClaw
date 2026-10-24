from unittest.mock import AsyncMock

import pytest
from tests.test_background_llm import background_case, tool_response

from app.dao.agent_dao import agent_dao
from app.services.llm import caller
from app.services.llm.background_guard import BackgroundCallOutcome, ScheduleLeaseLostError
from test_finish_protocol import _finish_response


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["factory", "provider", "tool"])
async def test_background_ownership_error_escapes_external_handlers(monkeypatch, phase):
    state = background_case(monkeypatch, [tool_response()], fallback=True)

    async def lost(*_args, **_kwargs):
        raise ScheduleLeaseLostError("expired")

    if phase == "factory":
        monkeypatch.setattr(caller, "create_fresh_llm_client_from_model", lost)
    elif phase == "provider":
        monkeypatch.setattr(state.client, "complete", lost)
    else:
        monkeypatch.setattr(caller, "execute_tool", lost)
    with pytest.raises(ScheduleLeaseLostError, match="expired"):
        await caller.call_agent_llm_with_tools(state.agent.id, "system", "user", turn=state.turn, guard=state.guard)
    assert len(state.created) == (0 if phase == "factory" else 1)
    assert state.usage == []
    assert state.client.closed is (phase != "factory")


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["dispatch", "confirmation"])
async def test_background_guard_failure_never_becomes_provider_failure(monkeypatch, phase):
    state = background_case(monkeypatch, [_finish_response("done")], fallback=True)
    original = state.guard.ensure_held

    def ensure_held():
        if len(state.created) > 0 and phase == "dispatch":
            raise RuntimeError("ownership check unavailable")
        original()

    async def confirm():
        if len(state.created) > 0 and phase == "confirmation":
            raise RuntimeError("ownership check unavailable")

    monkeypatch.setattr(state.guard, "ensure_held", ensure_held)
    monkeypatch.setattr(state.guard, "confirm", confirm)
    with pytest.raises(RuntimeError, match="ownership check unavailable"):
        await caller.call_agent_llm_with_tools(state.agent.id, "system", "user", turn=state.turn, guard=state.guard)
    assert state.created == ["fake-model"]
    assert state.client.messages_seen == []
    assert state.usage == []
    assert state.client.closed


@pytest.mark.asyncio
async def test_background_factory_revocation_closes_before_provider_dispatch(monkeypatch):
    state = background_case(monkeypatch, [_finish_response("done")], fallback=True)

    async def fresh(model, **_kwargs):
        state.created.append(model.model)
        state.guard.database_held = False
        return model, state.client

    monkeypatch.setattr(caller, "create_fresh_llm_client_from_model", fresh)
    with pytest.raises(ScheduleLeaseLostError):
        await caller.call_agent_llm_with_tools(state.agent.id, "system", "user", turn=state.turn, guard=state.guard)
    assert state.client.messages_seen == []
    assert state.client.closed
    assert state.usage == []


@pytest.mark.asyncio
async def test_background_close_revocation_prevents_failure_fallback(monkeypatch):
    state = background_case(monkeypatch, [RuntimeError("503 unavailable")], fallback=True)

    async def close():
        state.client.closed = True
        state.guard.database_held = False

    monkeypatch.setattr(state.client, "close", close)
    with pytest.raises(ScheduleLeaseLostError):
        await caller.call_agent_llm_with_tools(state.agent.id, "system", "user", turn=state.turn, guard=state.guard)
    assert state.created == ["fake-model"]
    assert state.usage == []
    assert state.client.closed


@pytest.mark.asyncio
async def test_background_usage_await_revocation_stops_next_round(monkeypatch):
    response = tool_response()
    response.tool_calls = []
    state = background_case(monkeypatch, [response, response, response], fallback=True)

    async def record(_agent_id, usage):
        state.usage.append(usage.total_tokens)
        state.guard.database_held = False

    monkeypatch.setattr(caller, "record_token_usage", record)
    with pytest.raises(ScheduleLeaseLostError):
        await caller.call_agent_llm_with_tools(state.agent.id, "system", "user", turn=state.turn, guard=state.guard)
    assert state.usage == [51]
    assert len(state.client.messages_seen) == 3
    assert state.created == ["fake-model"]
    assert state.client.closed


@pytest.mark.parametrize("guarded", [False, True])
async def test_daily_cap_stops_background_without_fallback(monkeypatch, guarded):
    # Given: an agent-wide quota refusal with a configured fallback.
    state = background_case(monkeypatch, [_finish_response("forbidden")], fallback=True)
    charge = AsyncMock(return_value=None)
    monkeypatch.setattr(agent_dao, "consume_llm_call", charge, raising=False)

    # When: either background contract reaches dispatch.
    result = await caller.call_agent_llm_with_tools(
        state.agent.id, "system", "user", turn=state.turn, guard=state.guard if guarded else None
    )

    # Then: quota denial is a failure, not an invitation to try another model.
    if guarded:
        assert isinstance(result, BackgroundCallOutcome)
        assert result.status == "failure"
    else:
        assert result != "forbidden"
    assert state.created == ["fake-model"]
    assert state.client.messages_seen == []
    assert state.client.closed


async def test_daily_cap_midloop_flushes_usage_without_fallback(monkeypatch):
    # Given: the budget permits only the first background provider request.
    state = background_case(monkeypatch, [tool_response(), _finish_response("forbidden")], fallback=True)
    charge = AsyncMock(side_effect=[state.agent, None])
    monkeypatch.setattr(agent_dao, "consume_llm_call", charge, raising=False)

    # When: a tool result would otherwise trigger the next request.
    result = await caller.call_agent_llm_with_tools(
        state.agent.id, "system", "user", turn=state.turn, guard=state.guard
    )

    # Then: prior usage is preserved and neither model dispatches again.
    assert result.status == "failure"
    assert len(state.client.messages_seen) == 1
    assert state.usage == [17]
    assert state.created == ["fake-model"]
    assert state.client.closed


@pytest.mark.parametrize("fallback_allowed", [False, True])
async def test_background_failover_is_charged_before_each_request(monkeypatch, fallback_allowed):
    # Given: a retryable primary failure and a separately charged fallback budget.
    state = background_case(monkeypatch, [RuntimeError("503 unavailable"), _finish_response("done")], fallback=True)
    charge = AsyncMock(side_effect=[state.agent, state.agent if fallback_allowed else None])
    monkeypatch.setattr(agent_dao, "consume_llm_call", charge, raising=False)
    complete = state.client.complete

    async def charged_complete(**kwargs):
        assert charge.await_count == len(state.client.messages_seen) + 1
        return await complete(**kwargs)

    monkeypatch.setattr(state.client, "complete", charged_complete)

    # When: primary fails before any tool side effect.
    result = await caller.call_agent_llm_with_tools(
        state.agent.id, "system", "user", turn=state.turn, guard=state.guard
    )

    # Then: each provider request consumes its own daily call.
    assert result.status == ("success" if fallback_allowed else "failure")
    assert charge.await_count == 2
    assert len(state.client.messages_seen) == (2 if fallback_allowed else 1)
    assert state.client.closed


async def test_background_rechecks_ownership_after_daily_charge(monkeypatch):
    # Given: ownership is lost while the quota transaction is awaited.
    state = background_case(monkeypatch, [_finish_response("forbidden")])

    async def charge(_agent_id):
        state.guard.held = False
        return state.agent

    monkeypatch.setattr(agent_dao, "consume_llm_call", charge, raising=False)

    # When: charging completes after ownership loss.
    with pytest.raises(ScheduleLeaseLostError):
        await caller.call_agent_llm_with_tools(state.agent.id, "system", "user", turn=state.turn, guard=state.guard)

    # Then: the charged request is never dispatched by the stale owner.
    assert state.client.messages_seen == []
    assert state.client.closed
