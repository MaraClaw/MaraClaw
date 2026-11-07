import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.config import get_settings
from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.services import openclaw_routing as routing
from app.services.shared_chat import shared_session_id


async def _checkpoint() -> None:
    reached = asyncio.Event()
    asyncio.get_running_loop().call_soon(reached.set)
    await asyncio.wait_for(reached.wait(), 1)


async def _join_wakes() -> None:
    while routing._wake_tasks:
        await asyncio.wait_for(asyncio.gather(*routing._wake_tasks), 2)


@dataclass
class Turns:
    agent: AgentRecord
    pending: list[GatewayMessageRecord] = field(default_factory=list)
    entered: dict[UUID, asyncio.Event] = field(default_factory=dict)
    release: dict[UUID, asyncio.Event] = field(default_factory=dict)
    seen: list[UUID] = field(default_factory=list)
    active: set[str] = field(default_factory=set)
    clock: int = 0

    def add(self, lane: str | None) -> GatewayMessageRecord:
        self.clock += 1
        row = GatewayMessageRecord(
            id=uuid4(),
            agent_id=self.agent.id,
            content="turn",
            conversation_id=lane,
            created_at=datetime(2026, 11, 1, tzinfo=UTC) + timedelta(seconds=self.clock),
        )
        self.pending.append(row)
        self.entered[row.id] = asyncio.Event()
        self.release[row.id] = asyncio.Event()
        return row

    @staticmethod
    def _key(row: GatewayMessageRecord) -> tuple[datetime, UUID]:
        assert row.created_at is not None
        return row.created_at, row.id

    def _oldest(self) -> list[GatewayMessageRecord]:
        heads: dict[str, GatewayMessageRecord] = {}
        for row in sorted(self.pending, key=self._key):
            heads.setdefault(row.conversation_id or str(row.id), row)
        return sorted(heads.values(), key=self._key)

    async def lane_heads_page(
        self, agent_id: UUID, limit: int, *, after: tuple[datetime, UUID] | None = None
    ) -> tuple[list[GatewayMessageRecord], tuple[datetime, UUID] | None]:
        assert agent_id == self.agent.id
        heads = [row for row in self._oldest() if after is None or self._key(row) > after]
        page = heads[:limit]
        return page, (self._key(page[-1]) if len(heads) > limit else None)

    async def lane_head(self, member: GatewayMessageRecord) -> GatewayMessageRecord | None:
        lane = member.conversation_id or str(member.id)
        return next((row for row in self._oldest() if (row.conversation_id or str(row.id)) == lane), None)

    async def execute(self, _agent: AgentRecord, _text: str, message_id: UUID) -> None:
        row = next(row for row in self.pending if row.id == message_id)
        lane = row.conversation_id or str(row.id)
        assert lane not in self.active
        self.active.add(lane)
        self.pending.remove(row)
        self.seen.append(row.id)
        self.entered[row.id].set()
        try:
            await self.release[row.id].wait()
        finally:
            self.active.remove(lane)

    async def started(self, row: GatewayMessageRecord) -> None:
        await asyncio.wait_for(self.entered[row.id].wait(), 1)

    def wake(self, row: GatewayMessageRecord) -> None:
        routing.wake_shared_lane(self.agent, row.id)


@pytest.fixture
async def turns(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Turns]:
    value = Turns(AgentRecord(id=uuid4(), name="Parallel", creator_id=uuid4()))
    routing.open_wake_admission()
    monkeypatch.setattr(routing.gateway_message_dao, "lane_heads_page", value.lane_heads_page)
    monkeypatch.setattr(routing.gateway_message_dao, "lane_head", value.lane_head)
    monkeypatch.setattr(routing, "_run_inbox_wake", value.execute)
    yield value
    for release in value.release.values():
        release.set()
    await routing.drain_wake_tasks(0)
    routing.open_wake_admission()
    assert not routing._lane_running
    assert not routing._lane_again
    assert not routing._wake_semaphores


@pytest.mark.parametrize("limit", [1, 2])
async def test_agent_parallel_limit_queues_remaining_lanes(
    turns: Turns, monkeypatch: pytest.MonkeyPatch, limit: int
) -> None:
    monkeypatch.setattr(get_settings(), "OPENCLAW_AGENT_MAX_PARALLEL_TURNS", limit)
    first, second, third = (turns.add(lane) for lane in ("one", "two", "three"))
    turns.wake(first)
    await turns.started(first)
    if limit == 2:
        await turns.started(second)
    await _checkpoint()
    assert turns.seen == [first.id, second.id][:limit]
    assert not turns.entered[third.id].is_set()

    turns.release[first.id].set()
    await turns.started(second if limit == 1 else third)
    if limit == 1:
        assert not turns.entered[third.id].is_set()
        turns.release[second.id].set()
        await turns.started(third)
    for release in turns.release.values():
        release.set()
    await _join_wakes()
    assert turns.seen == [first.id, second.id, third.id]


@pytest.mark.parametrize("shared", [False, True])
async def test_running_lane_coalesces_wakes_and_preserves_fifo(turns: Turns, shared: bool) -> None:
    lane = str(shared_session_id(turns.agent.id)) if shared else "ordinary"
    first = turns.add(lane)
    turns.wake(first)
    await turns.started(first)
    second = turns.add(lane)
    turns.wake(second)
    turns.wake(second)
    await _checkpoint()
    assert turns.seen == [first.id]
    turns.release[first.id].set()
    await turns.started(second)
    assert turns.seen == [first.id, second.id]
    turns.release[second.id].set()
    await _join_wakes()
    assert not turns.pending


async def test_new_lane_starts_while_another_lane_is_running(turns: Turns) -> None:
    first = turns.add("one")
    turns.wake(first)
    await turns.started(first)
    second = turns.add("two")
    turns.wake(second)
    await turns.started(second)
    assert turns.active == {"one", "two"}


async def test_lanes_past_a_page_of_running_lanes_still_start(turns: Turns, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "OPENCLAW_AGENT_MAX_PARALLEL_TURNS", 3)
    monkeypatch.setattr(routing, "_LANE_HEAD_PAGE", 1)
    running = turns.add("one")
    turns.wake(running)
    await turns.started(running)
    queued_behind_running = turns.add("one")
    two, three = turns.add("two"), turns.add("three")
    turns.wake(two)
    await turns.started(two)
    await turns.started(three)
    assert turns.active == {"one", "two", "three"}
    assert not turns.entered[queued_behind_running.id].is_set()


async def test_shutdown_joins_all_active_and_queued_lane_tasks(turns: Turns, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "OPENCLAW_AGENT_MAX_PARALLEL_TURNS", 2)
    first, second, third = (turns.add(lane) for lane in ("one", "two", "three"))
    turns.wake(first)
    await turns.started(first)
    await turns.started(second)
    draining = asyncio.create_task(routing.drain_wake_tasks(2))
    try:
        await _checkpoint()
        assert not draining.done()
        assert not routing._wake_admission.accepting
        turns.release[first.id].set()
        await _checkpoint()
        assert not draining.done()
        assert not turns.entered[third.id].is_set()
        turns.release[second.id].set()
        await asyncio.wait_for(draining, 2)
    finally:
        turns.release[first.id].set()
        turns.release[second.id].set()
        await asyncio.wait_for(draining, 2)
    assert not routing._wake_tasks
    assert turns.pending == [third]


async def test_conversationless_rows_have_independent_lanes(turns: Turns) -> None:
    first, second = turns.add(None), turns.add(None)
    turns.wake(first)
    await turns.started(first)
    await turns.started(second)
    assert turns.active == {str(first.id), str(second.id)}
