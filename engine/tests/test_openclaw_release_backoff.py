import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.services import (
    openclaw_gateway_rpc as rpc,
    openclaw_native_dispatch as dispatch,
    openclaw_release_backoff as backoff,
    openclaw_routing as routing,
)
from app.services.openclaw_native_capacity import LaneKey


@dataclass
class Clock:
    now: float = 100.0

    def __call__(self) -> float:
        return self.now


def test_consecutive_releases_double_the_wait_up_to_the_cap() -> None:
    clock = Clock()
    pacing = backoff.ReleaseBackoff(clock)
    lane = LaneKey(uuid4(), "conversation")
    assert [pacing.released(lane) for _ in range(7)] == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
    assert pacing.waiting(lane)
    clock.now += 29.9
    assert pacing.waiting(lane)
    clock.now += 0.1
    assert not pacing.waiting(lane)
    assert not pacing.waiting(LaneKey(lane.agent_id, "other"))


def test_settlement_and_long_quiet_reset_the_sequence() -> None:
    clock = Clock()
    pacing = backoff.ReleaseBackoff(clock)
    lane = LaneKey(uuid4(), uuid4())
    assert [pacing.released(lane) for _ in range(3)] == [1.0, 2.0, 4.0]
    pacing.settled(lane)
    assert not pacing.waiting(lane)
    assert pacing.released(lane) == 1.0
    clock.now += 1.0 + backoff.FORGET_AFTER_SECONDS
    assert pacing.released(lane) == 1.0


def test_prune_bounds_stale_lanes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backoff, "PRUNE_ABOVE", 3)
    clock = Clock()
    pacing = backoff.ReleaseBackoff(clock)
    stale = [LaneKey(uuid4(), "lane") for _ in range(3)]
    for lane in stale:
        _ = pacing.released(lane)
    clock.now += 1.0 + backoff.FORGET_AFTER_SECONDS
    fresh = LaneKey(uuid4(), "lane")
    _ = pacing.released(fresh)
    assert len(pacing) == 1
    assert pacing.waiting(fresh)


@pytest.mark.asyncio
async def test_refused_lane_is_not_reclaimed_until_its_wait_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    pacing = backoff.ReleaseBackoff(clock)
    monkeypatch.setattr(routing, "release_backoff", pacing)
    monkeypatch.setattr(dispatch, "release_backoff", pacing)
    agent = AgentRecord(id=uuid4(), name="Guest", creator_id=uuid4())
    row = GatewayMessageRecord(uuid4(), agent.id, "pending", conversation_id="one")
    outcomes = [
        rpc.NotAdmittedOutcome(str(row.id), "not_submitted", "guest_unavailable"),
        rpc.TerminalOutcome(str(row.id), str(row.id), "ok", "answer", None),
    ]
    executed = AsyncMock(side_effect=outcomes)
    released = AsyncMock(return_value=row)
    settled = AsyncMock(return_value=None)

    async def claim(_agent: AgentRecord, _message_id: object) -> GatewayMessageRecord:
        return row

    monkeypatch.setattr(routing.gateway_message_dao, "lane_heads_page", AsyncMock(return_value=([row], None)))
    monkeypatch.setattr(routing.gateway_message_dao, "lane_head", AsyncMock(return_value=row))
    monkeypatch.setattr(routing, "_claim_wake", claim)
    monkeypatch.setattr(rpc, "execute_gateway_turn", executed)
    monkeypatch.setattr(routing.gateway_message_dao, "release_delivery", released)
    from app.services import openclaw_gateway_delivery as delivery

    monkeypatch.setattr(delivery, "settle_native_report", settled)

    async def wake_and_drain() -> None:
        routing.wake_shared_lane(agent, row.id)
        while routing._wake_tasks:
            await asyncio.wait_for(asyncio.wait(routing._wake_tasks), 1)

    routing.open_wake_admission()
    await wake_and_drain()
    assert executed.await_count == 1
    released.assert_awaited_once()
    await wake_and_drain()
    assert executed.await_count == 1
    clock.now += 1.0
    await wake_and_drain()
    assert executed.await_count == 2
    settled.assert_awaited_once()
    assert not pacing.waiting(LaneKey.of(row))
