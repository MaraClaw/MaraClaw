from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.dao.agent_dao import agent_dao
from app.records.agent import AgentRecord
from app.services.llm import caller
from app.services.llm.types import LLMResponse
from test_finish_protocol import FakeStreamClient, _finish_response, _model


@pytest.fixture
def stream_case(monkeypatch: pytest.MonkeyPatch):
    agent = AgentRecord(id=uuid4(), name="Agent", creator_id=uuid4())
    client = FakeStreamClient([_finish_response("done")])
    charge = AsyncMock(return_value=agent)
    usage = AsyncMock()
    monkeypatch.setattr(agent_dao, "consume_llm_call", charge, raising=False)
    monkeypatch.setattr(caller, "_get_agent_config", AsyncMock(return_value=(5, None)))
    monkeypatch.setattr(caller, "_get_user_name", AsyncMock(return_value="User"))
    monkeypatch.setattr("app.services.agent_context.build_agent_context", AsyncMock(return_value=("", "")))
    monkeypatch.setattr(caller, "get_agent_tools_for_llm", AsyncMock(return_value=[]))
    monkeypatch.setattr(caller, "create_fresh_llm_client_from_model", AsyncMock(return_value=(_model(), client)))
    monkeypatch.setattr(caller, "record_token_usage", usage)
    return agent, client, charge, usage


async def test_caller_stops_before_dispatch_when_daily_cap_is_reached(stream_case):
    # Given: the atomic charge refuses the next provider request.
    agent, client, charge, usage = stream_case
    charge.return_value = None

    # When: an attributed chat turn reaches its first dispatch.
    reply = await caller.call_llm(_model(), [], agent.name, "", agent.id, agent.creator_id)

    # Then: the limit is not a provider error, and no request leaves the engine.
    assert not caller.is_retryable_error(reply)
    assert reply != "done"
    assert client.messages_seen == []
    assert client.closed
    usage.assert_not_awaited()


async def test_caller_flushes_tokens_and_stops_at_midloop_daily_cap(stream_case):
    # Given: one request succeeds, but the next round has no daily budget.
    agent, client, charge, usage = stream_case
    charge.side_effect = [agent, None]
    client.responses = [LLMResponse(content="continue", usage={"total_tokens": 17}), _finish_response("done")]

    # When: the finish protocol would otherwise request another round.
    reply = await caller.call_llm(_model(), [], agent.name, "", agent.id, agent.creator_id)

    # Then: the successful round is accounted for without a second dispatch.
    assert reply != "done"
    assert len(client.messages_seen) == 1
    assert client.closed
    assert usage.await_args is not None
    assert usage.await_args.args[1].total_tokens == 17


@pytest.mark.parametrize("fallback_allowed", [False, True])
async def test_failover_attempt_consumes_its_own_daily_call(stream_case, monkeypatch, fallback_allowed):
    # Given: a primary provider fails and the fallback may have remaining budget.
    agent, client, charge, _usage = stream_case
    charge.side_effect = [agent, agent if fallback_allowed else None]
    fallback = FakeStreamClient([_finish_response("fallback done")])

    async def broken_stream(**_kwargs):
        assert charge.await_count == 1
        raise RuntimeError("503 unavailable")

    client.stream = broken_stream
    monkeypatch.setattr(
        caller,
        "create_fresh_llm_client_from_model",
        AsyncMock(side_effect=[(_model(), client), (_model(), fallback)]),
    )

    # When: retryable provider failure enters the fallback lane.
    reply = await caller.call_llm_with_failover(
        _model(), replace(_model(), id=uuid4()), [], agent.name, "", agent.id, agent.creator_id
    )

    # Then: both attempts charge before dispatch, and refusal never reaches fallback.
    assert charge.await_count == 2
    assert len(fallback.messages_seen) == int(fallback_allowed)
    assert (reply == "fallback done") is fallback_allowed
    assert client.closed
    assert fallback.closed
