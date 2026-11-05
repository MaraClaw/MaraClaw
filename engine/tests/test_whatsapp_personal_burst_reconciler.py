import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

import pytest
from tests.test_whatsapp_personal_burst_reconciler_fixtures import (
    add_lane,
    burst_db,
    deliver,
    due_batch,
    due_lanes,
    occupy,
    real_pool,
    receipt_db,
)
from tests.test_whatsapp_personal_burst_store_fixtures import admit, age, anchor

from app.dao.gateway_message_dao import GatewayMessageDAO
from app.dao.personal_burst_dispatch_dao import personal_burst_dispatch_dao as dispatch
from app.db.connection import DbConnection
from app.db.session import get_connection, transaction
from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.services import openclaw_dispatch_reconciler as module
from app.services.openclaw_dispatch_reconciler import OpenClawDispatchReconciler

__all__ = ["burst_db", "real_pool", "receipt_db"]


@dataclass(slots=True)
class Wakes:
    """Records each wake request and whether a request-owned connection was ambient when it was made."""

    calls: list[tuple[UUID, UUID]] = field(default_factory=list)
    ambient: list[DbConnection | None] = field(default_factory=list)

    def __call__(self, agent: AgentRecord, message_id: UUID) -> None:
        self.calls.append((agent.id, message_id))
        self.ambient.append(get_connection())


def reconciler(wakes: Wakes, monkeypatch: pytest.MonkeyPatch, page_size: int = 50) -> OpenClawDispatchReconciler:
    async def page(
        limit: int, *, after: tuple[datetime, UUID] | None = None
    ) -> tuple[list[GatewayMessageRecord], tuple[datetime, UUID] | None]:
        assert limit == 50
        return await GatewayMessageDAO.pending_dispatch_page(module.gateway_message_dao, page_size, after=after)

    monkeypatch.setattr(module.gateway_message_dao, "pending_dispatch_page", page)
    monkeypatch.setattr(module, "wake_shared_lane", wakes)
    return OpenClawDispatchReconciler()


async def test_scan_wakes_every_due_lane_head_without_an_ambient_connection(burst_db, real_pool, monkeypatch):
    # Given three lanes whose bursts closed with no wake ever having been issued (lost post-commit wakes).
    f = burst_db
    lanes = await due_lanes(f, 3)
    wakes = Wakes()
    # When one scan runs, Then each lane's head is woken by agent and anchor, outside any transaction.
    assert await reconciler(wakes, monkeypatch).scan_once() == 3
    assert wakes.calls == [(lane.scope.agent_id, batch.gateway_id) for lane, batch in lanes]
    assert wakes.ambient == [None, None, None]


async def test_collecting_burst_is_not_woken_until_the_database_clock_says_due(burst_db, real_pool, monkeypatch):
    f = burst_db
    batch = await admit(f, "n1", "a")
    wakes = Wakes()
    scanner = reconciler(wakes, monkeypatch)
    assert await scanner.scan_once() == 0
    await age(f, batch.gateway_id, 10)
    assert await scanner.scan_once() == 1
    assert wakes.calls == [(f.scope.agent_id, batch.gateway_id)]


async def test_unready_real_head_is_not_bypassed_by_a_due_batch_behind_it(burst_db, real_pool, monkeypatch):
    f = burst_db
    unready = await admit(f, "n1", "a")
    async with transaction(f.db.first):
        _ = await anchor(f, content="web")
    _ = await due_batch(f, "n2")
    wakes = Wakes()
    assert await reconciler(wakes, monkeypatch).scan_once() == 0
    await age(f, unready.gateway_id, 10)
    assert await reconciler(wakes, monkeypatch).scan_once() == 1
    assert wakes.calls == [(f.scope.agent_id, unready.gateway_id)]


async def test_occupied_lanes_never_spend_the_page_so_later_lanes_run(burst_db, real_pool, monkeypatch):
    # Given two older lanes with an unreported delivered turn and one available lane, with a page of one.
    f = burst_db
    lanes = await due_lanes(f, 3)
    for lane, _ in lanes[:2]:
        _ = await occupy(lane)
    wakes = Wakes()
    # Then the first scan already reaches the available lane.
    assert await reconciler(wakes, monkeypatch, page_size=1).scan_once() == 1
    assert wakes.calls == [(lanes[2][0].scope.agent_id, lanes[2][1].gateway_id)]


async def test_blocked_heads_beyond_one_page_do_not_starve_later_lanes(burst_db, real_pool, monkeypatch):
    # Given five due lanes whose dispatch is blocked (the wake never makes progress) and a page of two.
    f = burst_db
    lanes = await due_lanes(f, 5)
    expected = [(lane.scope.agent_id, batch.gateway_id) for lane, batch in lanes]
    wakes = Wakes()
    scanner = reconciler(wakes, monkeypatch, page_size=2)
    # When scans repeat, Then the cursor advances through every lane before revisiting any.
    for _ in range(3):
        _ = await scanner.scan_once()
    assert wakes.calls == expected
    _ = await scanner.scan_once()
    assert wakes.calls[5:] == expected[:2]


async def test_earlier_work_that_becomes_due_behind_the_cursor_is_reached_after_the_wrap(
    burst_db, real_pool, monkeypatch
):
    f = burst_db
    lanes = await due_lanes(f, 3)
    wakes = Wakes()
    scanner = reconciler(wakes, monkeypatch, page_size=1)
    _ = await scanner.scan_once()
    newcomer = await add_lane(f)
    fresh = await due_batch(newcomer, "late")
    await age(newcomer, fresh.gateway_id, 100)
    async with transaction(f.db.first):
        await f.db.first.execute(
            "UPDATE gateway_messages SET created_at = created_at - interval '100 seconds' WHERE id = %(id)s",
            {"id": fresh.gateway_id},
        )
    for _ in range(3):
        _ = await scanner.scan_once()
    assert wakes.calls[1:] == [
        (lanes[1][0].scope.agent_id, lanes[1][1].gateway_id),
        (lanes[2][0].scope.agent_id, lanes[2][1].gateway_id),
        (newcomer.scope.agent_id, fresh.gateway_id),
    ]


async def test_restarted_scanner_starts_fresh_from_the_database_not_from_lost_memory(burst_db, real_pool, monkeypatch):
    f = burst_db
    lanes = await due_lanes(f, 3)
    first = Wakes()
    _ = await reconciler(first, monkeypatch, page_size=2).scan_once()
    assert len(first.calls) == 2
    restarted = Wakes()
    _ = await reconciler(restarted, monkeypatch, page_size=2).scan_once()
    assert restarted.calls == first.calls
    assert restarted.calls[0] == (lanes[0][0].scope.agent_id, lanes[0][1].gateway_id)


async def test_head_that_was_dispatched_between_scans_is_not_woken_again(burst_db, real_pool, monkeypatch):
    f = burst_db
    lanes = await due_lanes(f, 2)
    await deliver(lanes[0][0], lanes[0][1].gateway_id)
    wakes = Wakes()
    assert await reconciler(wakes, monkeypatch).scan_once() == 1
    assert wakes.calls == [(lanes[1][0].scope.agent_id, lanes[1][1].gateway_id)]


async def test_concurrent_reconcilers_duplicate_wakes_but_the_claim_arbiter_admits_one(
    burst_db, real_pool, monkeypatch
):
    # Given one due head seen by two reconcilers scanning at once, each of which wakes it.
    f = burst_db
    batch = await due_batch(f)
    wakes = Wakes()
    one = reconciler(wakes, monkeypatch)
    two = OpenClawDispatchReconciler()
    _ = await asyncio.gather(one.scan_once(), two.scan_once())
    assert wakes.calls == [(f.scope.agent_id, batch.gateway_id)] * 2

    # When both wakes run the claim step on separate connections (seal + claim under the shared-session lock).
    async def claim(connection: DbConnection) -> bool:
        async with transaction(connection):
            sealed = await dispatch.seal(batch.gateway_id)
            return sealed is not None and sealed.state == "sealed" and await dispatch.mark_claimed(batch.gateway_id)

    results = await asyncio.gather(claim(f.db.first), claim(f.db.second))
    # Then exactly one wins and the duplicate is a harmless no-op.
    assert sorted(results) == [False, True]
