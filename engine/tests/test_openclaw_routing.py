import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.records.llm import LLMModelRecord
from app.services import openclaw_hot_cache, openclaw_routing as routing
from app.services.agent_manager import agent_manager
from app.services.llm.turn import ModelBundle
from app.services.openclaw_update_gate import OpenClawUpdateInProgressError
from app.services.quota_guard import AgentExpired


@pytest.fixture
async def agent(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AgentRecord]:
    value = AgentRecord(id=uuid4(), name="Guest", creator_id=uuid4())
    openclaw_hot_cache.reset()
    routing.open_wake_admission()
    monkeypatch.setattr(routing, "assert_work_allowed", AsyncMock())
    monkeypatch.setattr(routing, "load_agent_for_new_work", AsyncMock(return_value=value))
    monkeypatch.setattr(routing, "ensure_agent_company_models", AsyncMock(return_value=value))
    yield value
    await routing.drain_wake_tasks(0)
    routing.open_wake_admission()
    openclaw_hot_cache.reset()


@pytest.mark.parametrize(
    ("content", "slot", "ref"),
    [
        ("hi", "secondary", "anthropic/haiku"),
        ("Can you help me with this tomorrow morning please", "primary", "anthropic/opus"),
    ],
)
async def test_preparation_selects_without_guest_write_or_classifier(
    agent: AgentRecord, monkeypatch: pytest.MonkeyPatch, content: str, slot: str, ref: str
) -> None:
    primary = LLMModelRecord(uuid4(), "anthropic", "opus", "key", "Primary")
    secondary = LLMModelRecord(uuid4(), "anthropic", "haiku", "key", "Secondary")
    monkeypatch.setattr(routing, "load_agent_model_bundle", AsyncMock(return_value=ModelBundle(primary, secondary)))
    writer = Mock(side_effect=AssertionError("enqueue must not alter an active turn's model"))
    classifier = AsyncMock(side_effect=AssertionError("enqueue must remain responsive"))
    monkeypatch.setattr(agent_manager, "write_guest_config", writer)
    monkeypatch.setattr("app.services.llm.router._classify_with_llm", classifier)

    prepared = await routing.prepare_openclaw_message(agent=agent, content=content)

    assert (prepared.selected_slot, prepared.guest_model_ref) == (slot, ref)
    writer.assert_not_called()
    classifier.assert_not_awaited()


async def test_preparation_gates_before_model_work(agent: AgentRecord, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routing, "assert_work_allowed", AsyncMock(side_effect=OpenClawUpdateInProgressError()))
    load = AsyncMock()
    monkeypatch.setattr(routing, "load_agent_model_bundle", load)
    with pytest.raises(OpenClawUpdateInProgressError):
        await routing.prepare_openclaw_message(agent=agent, content="hi")
    load.assert_not_awaited()


async def test_preparation_reuses_eligible_bundle(agent: AgentRecord, monkeypatch: pytest.MonkeyPatch) -> None:
    model = LLMModelRecord(uuid4(), "anthropic", "opus", "key", "Primary")
    load = AsyncMock(return_value=ModelBundle(primary=model))
    monkeypatch.setattr(routing, "load_agent_model_bundle", load)
    await routing.prepare_openclaw_message(agent=agent, content="first")
    await routing.prepare_openclaw_message(agent=agent, content="second")
    load.assert_awaited_once_with(agent)


async def test_empty_bundle_is_rejected_and_never_cached(agent: AgentRecord, monkeypatch: pytest.MonkeyPatch) -> None:
    model = LLMModelRecord(uuid4(), "anthropic", "opus", "key", "Primary")
    load = AsyncMock(side_effect=[ModelBundle(), ModelBundle(primary=model)])
    monkeypatch.setattr(routing, "load_agent_model_bundle", load)
    with pytest.raises(routing.NoCompanyModelError):
        await routing.prepare_openclaw_message(agent=agent, content="hi")
    prepared = await routing.prepare_openclaw_message(agent=agent, content="hi")
    assert load.await_count == 2
    assert prepared.guest_model_ref == "anthropic/opus"


async def test_enqueue_returns_while_owned_turn_is_running(agent: AgentRecord, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = routing.PreparedOpenClawMessage(agent, "hello", "primary", "anthropic/opus", "complex", "rule")
    pending = GatewayMessageRecord(id=uuid4(), agent_id=agent.id, content="hello")
    started, release = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def transaction() -> AsyncGenerator[None]:
        yield None

    async def execute(*_args: object) -> None:
        started.set()
        await release.wait()

    monkeypatch.setattr(routing, "connection_ctx", transaction)
    monkeypatch.setattr(routing, "prepare_openclaw_message", AsyncMock(return_value=prepared))
    monkeypatch.setattr(routing, "_insert_prepared_message", AsyncMock(return_value=pending))
    monkeypatch.setattr(routing.gateway_message_dao, "lane_heads_page", AsyncMock(return_value=([pending], None)))
    monkeypatch.setattr(routing, "_run_inbox_wake", execute)
    try:
        result = await routing.enqueue_openclaw_message(agent=agent, content="hello")
        await asyncio.wait_for(started.wait(), 1)
        assert result is pending
        assert routing._wake_tasks
        assert not release.is_set()
    finally:
        release.set()
        await routing.drain_wake_tasks(1)


@pytest.mark.parametrize("refusal", [OpenClawUpdateInProgressError(), AgentExpired("Guest")])
async def test_wake_leaves_row_pending_when_the_claim_refuses_after_the_gate_check(
    agent: AgentRecord, monkeypatch: pytest.MonkeyPatch, refusal: Exception
) -> None:
    claim = AsyncMock(side_effect=refusal)
    execute = AsyncMock(side_effect=AssertionError("a refused claim must not execute"))
    release = AsyncMock(side_effect=AssertionError("nothing was claimed, so nothing is released"))
    monkeypatch.setattr(routing, "_claim_wake", claim)
    monkeypatch.setattr("app.services.openclaw_gateway_rpc.execute_gateway_turn", execute)
    monkeypatch.setattr(routing.gateway_message_dao, "release_delivery", release)

    await routing._run_inbox_wake(agent, "", uuid4())

    claim.assert_awaited_once()
    load_agent = routing.load_agent_for_new_work
    assert isinstance(load_agent, AsyncMock)
    load_agent.assert_not_awaited()
    execute.assert_not_awaited()
    release.assert_not_awaited()
