import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.services import openclaw_routing as routing


@pytest.fixture
async def agent() -> AsyncIterator[AgentRecord]:
    routing.open_wake_admission()
    yield AgentRecord(id=uuid4(), name="Guest", creator_id=uuid4())
    await routing.drain_wake_tasks(0)
    routing.open_wake_admission()


async def test_closed_admission_never_starts_a_drain(agent: AgentRecord, monkeypatch: pytest.MonkeyPatch) -> None:
    pending = AsyncMock()
    monkeypatch.setattr(routing.gateway_message_dao, "lane_heads_page", pending)
    routing.close_wake_admission()
    routing.wake_shared_lane(agent, uuid4())
    await asyncio.sleep(0)
    pending.assert_not_awaited()
    assert not routing._wake_tasks


async def test_one_attempt_per_lane_in_each_pass(agent: AgentRecord, monkeypatch: pytest.MonkeyPatch) -> None:
    first = GatewayMessageRecord(id=uuid4(), agent_id=agent.id, content="first", conversation_id="shared")
    younger = replace(first, id=uuid4(), content="younger")
    other = replace(first, id=uuid4(), content="other", conversation_id="group")
    cursor = (datetime(2026, 11, 1, tzinfo=UTC), first.id)
    run = AsyncMock()
    # The lane head moved between pages, so the second page carries the same lane again.
    pages = AsyncMock(side_effect=[([first], cursor), ([younger, other], None)])
    monkeypatch.setattr(routing.gateway_message_dao, "lane_heads_page", pages)
    monkeypatch.setattr(routing, "_run_inbox_wake", run)
    routing.wake_shared_lane(agent, younger.id)
    await asyncio.gather(*routing._wake_tasks)
    assert [call.args[2] for call in run.await_args_list] == [first.id, other.id]


async def test_dirty_wake_drains_new_work_without_parallel_turns(
    agent: AgentRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = GatewayMessageRecord(id=uuid4(), agent_id=agent.id, content="first", conversation_id="thread")
    second = replace(first, id=uuid4(), content="second")
    entered, release = asyncio.Event(), asyncio.Event()
    rescanned = asyncio.Event()
    seen: list[UUID] = []

    async def execute(_agent: AgentRecord, _text: str, message_id: UUID) -> None:
        seen.append(message_id)
        if message_id == first.id:
            entered.set()
            await release.wait()

    async def heads(_agent_id: UUID, _limit: int, *, after: object = None) -> tuple[list[GatewayMessageRecord], None]:
        if not seen:
            return [first], None
        rescanned.set()
        return ([second] if second.id not in seen else []), None

    async def lane_head(_member: GatewayMessageRecord) -> GatewayMessageRecord | None:
        return second if second.id not in seen else None

    monkeypatch.setattr(routing.gateway_message_dao, "lane_heads_page", heads)
    monkeypatch.setattr(routing.gateway_message_dao, "lane_head", lane_head)
    monkeypatch.setattr(routing, "_run_inbox_wake", execute)
    routing.wake_shared_lane(agent, first.id)
    try:
        await asyncio.wait_for(entered.wait(), 1)
        routing.wake_shared_lane(agent, second.id)
        routing.wake_shared_lane(agent, second.id)
        await asyncio.wait_for(rescanned.wait(), 1)
        assert seen == [first.id]
    finally:
        release.set()
        await asyncio.gather(*routing._wake_tasks)
    assert seen == [first.id, second.id]


async def test_shutdown_stops_dirty_followup_and_younger_work(
    agent: AgentRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = GatewayMessageRecord(id=uuid4(), agent_id=agent.id, content="first", conversation_id="one")
    second = replace(first, id=uuid4(), conversation_id="two")
    seen: list[UUID] = []

    async def execute(_agent: AgentRecord, _text: str, message_id: UUID) -> None:
        seen.append(message_id)
        routing.wake_shared_lane(agent, second.id)
        routing.close_wake_admission()

    monkeypatch.setattr(routing.gateway_message_dao, "lane_heads_page", AsyncMock(return_value=([first, second], None)))
    monkeypatch.setattr(routing, "_run_inbox_wake", execute)
    routing.wake_shared_lane(agent, first.id)
    await asyncio.gather(*routing._wake_tasks)
    assert seen == [first.id]
    assert agent.id not in routing._wake_running
    assert agent.id not in routing._wake_again


async def test_graceful_drain_joins_active_turn(agent: AgentRecord, monkeypatch: pytest.MonkeyPatch) -> None:
    row = GatewayMessageRecord(id=uuid4(), agent_id=agent.id, content="one")
    entered, finish, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def execute(*_args: object) -> None:
        entered.set()
        await finish.wait()
        completed.set()

    monkeypatch.setattr(routing.gateway_message_dao, "lane_heads_page", AsyncMock(return_value=([row], None)))
    monkeypatch.setattr(routing, "_run_inbox_wake", execute)
    routing.wake_shared_lane(agent, row.id)
    await asyncio.wait_for(entered.wait(), 1)
    draining = asyncio.create_task(routing.drain_wake_tasks(1))
    try:
        await asyncio.sleep(0)
        assert not draining.done()
    finally:
        finish.set()
        await draining
    assert completed.is_set()
    assert not routing._wake_tasks


async def test_repeated_cancel_drain_reaps_only_owned_work(agent: AgentRecord, monkeypatch: pytest.MonkeyPatch) -> None:
    row = GatewayMessageRecord(id=uuid4(), agent_id=agent.id, content="one")
    entered, cleaning, finish, reaped = (asyncio.Event() for _ in range(4))

    async def execute(*_args: object) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaning.set()
            await finish.wait()
            reaped.set()
            raise

    monkeypatch.setattr(routing.gateway_message_dao, "lane_heads_page", AsyncMock(return_value=([row], None)))
    monkeypatch.setattr(routing, "_run_inbox_wake", execute)
    foreign = asyncio.create_task(asyncio.Event().wait())
    routing.wake_shared_lane(agent, row.id)
    await asyncio.wait_for(entered.wait(), 1)
    draining = asyncio.create_task(routing.drain_wake_tasks(0))
    try:
        await asyncio.wait_for(cleaning.wait(), 1)
        draining.cancel()
        await asyncio.sleep(0)
        draining.cancel()
        await asyncio.sleep(0)
        assert not draining.done()
        assert not foreign.done()
    finally:
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await draining
        foreign.cancel()
        await asyncio.gather(foreign, return_exceptions=True)
    assert reaped.is_set()
    assert not routing._wake_tasks
    assert agent.id not in routing._wake_running
