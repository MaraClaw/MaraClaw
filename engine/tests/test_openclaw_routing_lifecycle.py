import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from app.config import get_settings
from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.services import openclaw_gateway_delivery as delivery, openclaw_gateway_rpc as rpc, openclaw_routing as routing
from app.services.openclaw_native_capacity import native_capacity


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
    monkeypatch.setattr(
        routing.gateway_message_dao,
        "lane_head",
        AsyncMock(side_effect=lambda row: first if row.conversation_id == "shared" else other),
    )
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
        return next((row for row in (first, second) if row.id not in seen), None)

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
        while routing._wake_tasks:
            await asyncio.wait_for(asyncio.wait(routing._wake_tasks), 1)
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
    monkeypatch.setattr(
        routing.gateway_message_dao,
        "lane_head",
        AsyncMock(side_effect=lambda row: first if row.conversation_id == "one" else second),
    )
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
    monkeypatch.setattr(routing.gateway_message_dao, "lane_head", AsyncMock(return_value=row))
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
    monkeypatch.setattr(routing.gateway_message_dao, "lane_head", AsyncMock(return_value=row))
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


async def test_process_slot_is_reserved_before_claim_and_held_through_rpc(agent, monkeypatch):
    monkeypatch.setattr(get_settings(), "OPENCLAW_PROCESS_MAX_PARALLEL_TURNS", 1)
    first = GatewayMessageRecord(uuid4(), agent.id, "first", conversation_id="one")
    second = replace(first, id=uuid4(), conversation_id="two")
    rows = {"one": first, "two": second}
    entered = {row.id: asyncio.Event() for row in rows.values()}
    release = {row.id: asyncio.Event() for row in rows.values()}
    claims: list[UUID] = []

    async def claim(_agent, message_id):
        assert native_capacity().active == {agent.id: 1}
        claims.append(message_id)
        return next(row for row in rows.values() if row.id == message_id)

    async def execute(_agent, message, **_kwargs):
        entered[message.id].set()
        await release[message.id].wait()
        return rpc.UncertainOutcome(str(message.id), "unknown", None, "connection_lost")

    monkeypatch.setattr(routing.gateway_message_dao, "lane_heads_page", AsyncMock(return_value=([first, second], None)))
    monkeypatch.setattr(
        routing.gateway_message_dao, "lane_head", AsyncMock(side_effect=lambda row: rows[row.conversation_id])
    )
    monkeypatch.setattr(routing, "_claim_wake", claim)
    monkeypatch.setattr(rpc, "execute_gateway_turn", execute)
    released = AsyncMock(side_effect=AssertionError("uncertain claims must remain delivered"))
    monkeypatch.setattr(routing.gateway_message_dao, "release_delivery", released)
    routing.wake_shared_lane(agent, first.id)
    try:
        await asyncio.wait_for(entered[first.id].wait(), 1)
        assert claims == [first.id]
        assert len(routing._wake_tasks) == 1
        release[first.id].set()
        await asyncio.wait_for(entered[second.id].wait(), 1)
        assert claims == [first.id, second.id]
    finally:
        for event in release.values():
            event.set()
        while routing._wake_tasks:
            await asyncio.wait_for(asyncio.wait(routing._wake_tasks), 1)
    released.assert_not_awaited()


@pytest.mark.parametrize("kind", ["refused", "terminal"])
async def test_returned_rpc_releases_capacity_before_persistence(agent, monkeypatch, kind):
    monkeypatch.setattr(get_settings(), "OPENCLAW_PROCESS_MAX_PARALLEL_TURNS", 1)
    first = GatewayMessageRecord(uuid4(), agent.id, "first", conversation_id="one")
    second = replace(first, id=uuid4(), conversation_id="two")
    rows = {"one": first, "two": second}
    persisting, unblock, second_started = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def claim(_agent, message_id):
        return next(row for row in rows.values() if row.id == message_id)

    async def execute(_agent, message, **_kwargs):
        if message.id == second.id:
            second_started.set()
            return rpc.UncertainOutcome(str(message.id), "unknown", None, "unknown")
        if kind == "refused":
            return rpc.NotAdmittedOutcome(str(message.id), "not_submitted", "guest_unavailable")
        return rpc.TerminalOutcome(str(message.id), str(message.id), "ok", "answer", None)

    async def persist(*_args, **_kwargs):
        persisting.set()
        await unblock.wait()

    monkeypatch.setattr(routing.gateway_message_dao, "lane_heads_page", AsyncMock(return_value=([first, second], None)))
    monkeypatch.setattr(
        routing.gateway_message_dao, "lane_head", AsyncMock(side_effect=lambda row: rows[row.conversation_id])
    )
    monkeypatch.setattr(routing, "_claim_wake", claim)
    monkeypatch.setattr(rpc, "execute_gateway_turn", execute)
    monkeypatch.setattr(routing.gateway_message_dao, "release_delivery", persist)
    monkeypatch.setattr(delivery, "settle_native_report", persist)
    routing.wake_shared_lane(agent, first.id)
    try:
        await asyncio.wait_for(persisting.wait(), 1)
        await asyncio.wait_for(second_started.wait(), 1)
        assert not unblock.is_set()
    finally:
        unblock.set()
        while routing._wake_tasks:
            await asyncio.wait_for(asyncio.wait(routing._wake_tasks), 1)


@pytest.mark.parametrize("phase", ["scan", "turn"])
async def test_cancel_before_first_task_step_clears_owned_admission(agent, monkeypatch, phase):
    row = GatewayMessageRecord(uuid4(), agent.id, "pending", conversation_id="one")
    reads = AsyncMock(side_effect=AssertionError("cancelled tasks must not read or claim"))
    monkeypatch.setattr(routing.gateway_message_dao, "lane_heads_page", reads)
    monkeypatch.setattr(routing.gateway_message_dao, "lane_head", reads)
    capacity = native_capacity()
    if phase == "scan":
        routing.wake_shared_lane(agent, row.id)
    else:
        routing._start_lane(agent, row)
    routing.close_wake_admission()
    for task in routing._wake_tasks:
        task.cancel()
    await routing.drain_wake_tasks(0)
    assert not routing._wake_tasks
    assert not routing._lane_running
    assert not routing._wake_running
    assert not capacity.active
    assert not capacity.waiting
    reads.assert_not_awaited()


async def test_empty_claim_releases_capacity_to_the_next_lane(agent, monkeypatch):
    monkeypatch.setattr(get_settings(), "OPENCLAW_PROCESS_MAX_PARALLEL_TURNS", 1)
    first = GatewayMessageRecord(uuid4(), agent.id, "first", conversation_id="one")
    second = replace(first, id=uuid4(), conversation_id="two")
    rows = {"one": first, "two": second}
    claim = AsyncMock(side_effect=[None, second])
    execute = AsyncMock(return_value=rpc.UncertainOutcome(str(second.id), "unknown", None, "unknown"))
    monkeypatch.setattr(routing, "_claim_wake", claim)
    monkeypatch.setattr(rpc, "execute_gateway_turn", execute)
    monkeypatch.setattr(
        routing.gateway_message_dao, "lane_head", AsyncMock(side_effect=lambda row: rows[row.conversation_id])
    )
    routing._start_lane(agent, first)
    routing._start_lane(agent, second)
    while routing._wake_tasks:
        await asyncio.wait_for(asyncio.wait(routing._wake_tasks), 1)
    assert [call.args[1] for call in claim.await_args_list] == [first.id, second.id]
    execute.assert_awaited_once()
