import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from uuid import uuid4

import pytest

from app.records.agent import AgentRecord
from app.services.llm import caller
from app.services.llm.background_guard import BackgroundCallOutcome, ScheduleLeaseLostError
from app.services.llm.base import ToolDefinition
from app.services.llm.turn import TurnContext
from app.services.llm.types import LLMMessage, LLMResponse
from test_finish_protocol import (
    _async_return,
    _finish_response,
    _finish_response_with_arguments,
    _model,
)


class BackgroundGuard:
    def __init__(self) -> None:
        self.held: bool = True
        self.database_held: bool = True
        self.successful_confirms: int = 0

    def ensure_held(self) -> None:
        if not self.held:
            raise ScheduleLeaseLostError("expired")

    async def confirm(self) -> None:
        self.held = self.held and self.database_held
        self.ensure_held()


class BackgroundClient:
    def __init__(self, responses: Sequence[LLMResponse | BaseException]) -> None:
        self.responses: list[LLMResponse | BaseException] = list(responses)
        self.messages_seen: list[list[LLMMessage]] = []
        self.tools_seen: list[list[ToolDefinition]] = []
        self.closed: bool = False
        self.entered: asyncio.Event = asyncio.Event()
        self.release: asyncio.Event = asyncio.Event()
        self.paused: bool = False
        self.pause_at: int = 1

    async def complete(
        self, *, messages: list[LLMMessage], tools: list[ToolDefinition] | None = None, **_kwargs: object
    ) -> LLMResponse:
        self.messages_seen.append(list(messages))
        self.tools_seen.append(tools or [])
        self.entered.set()
        if self.paused and len(self.messages_seen) == self.pause_at:
            await self.release.wait()
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    async def close(self) -> None:
        self.closed = True


@dataclass
class BackgroundCase:
    agent: AgentRecord
    turn: TurnContext
    client: BackgroundClient
    guard: BackgroundGuard
    created: list[str] = field(default_factory=list)
    usage: list[int] = field(default_factory=list)
    executed: list[str] = field(default_factory=list)


def tool_response(count: int = 1) -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[
            {"id": f"call-{index}", "function": {"name": "dummy", "arguments": "{}"}} for index in range(count)
        ],
        usage={"total_tokens": 17},
    )


def background_case(
    monkeypatch: pytest.MonkeyPatch, responses: Sequence[LLMResponse | BaseException], *, fallback: bool = False
) -> BackgroundCase:
    tenant_id = uuid4()
    model = replace(_model(), tenant_id=tenant_id)
    agent = AgentRecord(id=uuid4(), name="Agent", creator_id=uuid4(), tenant_id=tenant_id)
    turn = TurnContext(
        agent=agent,
        primary_model=model,
        fallback_model=replace(model, id=uuid4(), model="fallback") if fallback else None,
    )
    client = BackgroundClient(responses)
    state = BackgroundCase(agent, turn, client, BackgroundGuard())

    async def fresh(selected, **_kwargs):
        state.created.append(selected.model)
        return selected, client

    async def record(_agent_id, usage):
        state.usage.append(usage.total_tokens)

    async def execute(name, *_args, **_kwargs):
        state.executed.append(name)
        return "result"

    tools = [{"type": "function", "function": {"name": "dummy", "parameters": {}}}]
    monkeypatch.setattr(caller, "get_agent_tools_for_llm", lambda *_args: _async_return(tools))
    monkeypatch.setattr(caller, "create_fresh_llm_client_from_model", fresh)
    monkeypatch.setattr(caller, "record_token_usage", record)
    monkeypatch.setattr(caller, "execute_tool", execute)
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("guarded", [False, True], ids=["legacy-string", "typed"])
async def test_background_finish_contract(monkeypatch, guarded):
    state = background_case(monkeypatch, [_finish_response("[Error] is valid finished content")])
    if guarded:
        result = await caller.call_agent_llm_with_tools(
            state.agent.id, "system", "user", turn=state.turn, guard=state.guard
        )
    else:
        result = await caller.call_agent_llm_with_tools(state.agent.id, "system", "user", turn=state.turn)
    expected = "[Error] is valid finished content"
    assert result == (BackgroundCallOutcome("success", expected) if guarded else expected)
    assert len(state.usage) == 1
    assert state.client.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("guarded", [False, True], ids=["legacy-string", "typed"])
async def test_background_provider_failure_contract(monkeypatch, guarded):
    state = background_case(monkeypatch, [RuntimeError("503 unavailable")])
    result = await caller.call_agent_llm_with_tools(
        state.agent.id, "system", "user", turn=state.turn, guard=state.guard if guarded else None
    )
    expected = "[Error] 503 unavailable"
    assert result == (BackgroundCallOutcome("failure", expected) if guarded else expected)
    assert state.usage == []
    assert state.client.closed


@pytest.mark.asyncio
async def test_background_requires_valid_finish_before_success(monkeypatch):
    invalid = _finish_response_with_arguments("{}")
    state = background_case(monkeypatch, [LLMResponse(content="not finished"), invalid, _finish_response("done")])
    result = await caller.call_agent_llm_with_tools(
        state.agent.id, "system", "user", turn=state.turn, guard=state.guard
    )
    assert result == BackgroundCallOutcome("success", "done")
    assert len(state.client.messages_seen) == 3
    assert any(message.role == "tool" for message in state.client.messages_seen[2])
    assert state.executed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tools_started", [False, True])
async def test_background_fallback_only_before_tools(monkeypatch, tools_started):
    responses = ([tool_response()] if tools_started else []) + [
        RuntimeError("503 unavailable"),
        _finish_response("done"),
    ]
    state = background_case(monkeypatch, responses, fallback=True)
    result = await caller.call_agent_llm_with_tools(
        state.agent.id, "system", "user", turn=state.turn, guard=state.guard
    )
    assert result.status == ("failure" if tools_started else "success")
    assert state.created == (["fake-model"] if tools_started else ["fake-model", "fallback"])
    assert state.executed == (["dummy"] if tools_started else [])


@pytest.mark.asyncio
async def test_background_rejects_foreign_tenant_model(monkeypatch):
    state = background_case(monkeypatch, [_finish_response("forbidden")])
    state.turn.primary_model = replace(_model(), tenant_id=uuid4())
    result = await caller.call_agent_llm_with_tools(
        state.agent.id, "system", "user", turn=state.turn, guard=state.guard
    )
    assert result == BackgroundCallOutcome("failure", "⚠️ Agent has no LLM model configured")
    assert state.created == []


@pytest.mark.asyncio
async def test_background_exhaustion_is_failure(monkeypatch):
    state = background_case(monkeypatch, [LLMResponse(content="not finished")])
    result = await caller.call_agent_llm_with_tools(
        state.agent.id, "system", "user", max_rounds=1, turn=state.turn, guard=state.guard
    )
    assert result == BackgroundCallOutcome("failure", "[Error] Too many tool call rounds")
    assert state.client.closed


@pytest.mark.asyncio
async def test_background_closes_client_on_cancellation(monkeypatch):
    state = background_case(monkeypatch, [_finish_response("done")])
    state.client.paused = True
    task = asyncio.create_task(
        caller.call_agent_llm_with_tools(state.agent.id, "system", "user", turn=state.turn, guard=state.guard)
    )
    await asyncio.wait_for(state.client.entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert state.client.closed
    assert state.usage == []
