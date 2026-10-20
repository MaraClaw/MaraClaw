import pytest
from tests.test_background_llm import background_case

from app.services.llm import caller
from app.services.llm.background_guard import BackgroundCallOutcome, ScheduleLeaseLostError
from app.services.llm.types import LLMResponse
from test_finish_protocol import _async_return, _finish_response


@pytest.mark.asyncio
@pytest.mark.parametrize("guarded", [False, True])
async def test_background_provider_failure_flushes_prior_usage_once(monkeypatch, guarded):
    state = background_case(
        monkeypatch, [LLMResponse(content="continue", usage={"total_tokens": 17}), RuntimeError("503 unavailable")]
    )
    result = await caller.call_agent_llm_with_tools(
        state.agent.id, "system", "user", turn=state.turn, guard=state.guard if guarded else None
    )
    assert result == (
        BackgroundCallOutcome("failure", "[Error] 503 unavailable") if guarded else "[Error] 503 unavailable"
    )
    assert state.usage == [17]
    assert state.client.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["finish", "exhaustion", "provider-error", "mid-loop"])
async def test_background_confirms_before_each_usage_flush(monkeypatch, path):
    plain = LLMResponse(content="continue", usage={"total_tokens": 17})
    responses = {
        "finish": [_finish_response("done")],
        "exhaustion": [plain],
        "provider-error": [plain, RuntimeError("503 unavailable")],
        "mid-loop": [plain, plain, plain],
    }[path]
    state = background_case(monkeypatch, responses)
    confirm = state.guard.confirm

    async def deny_flush():
        if path == "provider-error":
            flushing = not state.client.responses
        elif path == "mid-loop":
            flushing = len(state.client.messages_seen) == 3
        else:
            flushing = bool(state.client.messages_seen)
        if flushing:
            successful_confirms = state.guard.successful_confirms
            state.guard.successful_confirms = successful_confirms + 1
            if successful_confirms == 1:
                state.guard.database_held = False
        await confirm()

    monkeypatch.setattr(state.guard, "confirm", deny_flush)
    rounds = 4 if path == "mid-loop" else (1 if path == "exhaustion" else 3)
    with pytest.raises(ScheduleLeaseLostError, match="expired"):
        await caller.call_agent_llm_with_tools(
            state.agent.id, "system", "user", max_rounds=rounds, turn=state.turn, guard=state.guard
        )
    assert state.usage == []
    assert state.client.closed


@pytest.mark.asyncio
async def test_background_token_limit_flushes_once_and_stops(monkeypatch):
    response = LLMResponse(content="continue", usage={"total_tokens": 17})
    state = background_case(monkeypatch, [response, response, response])
    monkeypatch.setattr(caller, "_get_agent_config", lambda *_args: _async_return((50, "limit reached")))
    result = await caller.call_agent_llm_with_tools(
        state.agent.id, "system", "user", turn=state.turn, guard=state.guard
    )
    assert result == BackgroundCallOutcome("failure", "limit reached")
    assert state.usage == [51]
    assert len(state.client.messages_seen) == 3
    assert state.client.closed


@pytest.mark.asyncio
async def test_background_usage_write_failure_is_not_retried(monkeypatch):
    state = background_case(monkeypatch, [_finish_response("done")], fallback=True)

    async def record(_agent_id, usage):
        state.usage.append(usage.total_tokens)
        raise RuntimeError("usage database unavailable")

    monkeypatch.setattr(caller, "record_token_usage", record)
    with pytest.raises(RuntimeError, match="usage database unavailable"):
        await caller.call_agent_llm_with_tools(state.agent.id, "system", "user", turn=state.turn, guard=state.guard)
    assert len(state.usage) == 1
    assert state.created == ["fake-model"]
    assert state.client.closed
