import asyncio
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.dao.schedule_dao import agent_schedule_dao
from app.db.session import connection_ctx
from app.records.schedule import AgentScheduleRecord
from schedule_occurrence_persistence_fakes import SLOT_COLUMNS
from test_schedule_dao import NEXT, NOW, _next_run, _Pool, _Raw, _schedule, pool

__all__ = ["pool"]


@pytest.mark.parametrize("notify", [False, True])
async def test_reclaim_preserves_snapshot_and_advance_after_unstarted_interruption(pool: _Pool, notify: bool) -> None:
    row = replace(_schedule(), notify_on_failure=notify)
    pool.rows[row.id] = row
    (first,) = await agent_schedule_dao.claim_due(NOW, _next_run)
    edited = replace(
        pool.rows[row.id],
        instruction="Edited",
        cron_expr="invalid",
        timezone="Not/A_Zone",
        notify_on_failure=not notify,
    )
    pool.rows[row.id] = edited
    compute = Mock(side_effect=AssertionError("Recovery must not recompute the cron"))
    (recovered,) = await agent_schedule_dao.claim_due(NOW + timedelta(minutes=5), compute)
    assert recovered == replace(edited, lease_token=recovered.lease_token, lease_expires_at=NOW + timedelta(minutes=10))
    assert recovered.lease_token is not None
    assert recovered.lease_token != first.lease_token
    assert recovered.pending_due_at == row.next_run_at
    assert recovered.pending_instruction == row.instruction
    assert recovered.pending_notify_on_failure is notify
    assert recovered.next_run_at == NEXT
    compute.assert_not_called()


async def test_live_slot_blocks_newer_due_occurrences(pool: _Pool) -> None:
    row = _schedule()
    pool.rows[row.id] = row
    (first,) = await agent_schedule_dao.claim_due(NOW, _next_run)
    occupied = replace(first, next_run_at=NOW - timedelta(seconds=1))
    pool.rows[row.id] = occupied
    assert await agent_schedule_dao.claim_due(NOW + timedelta(minutes=4), _next_run) == []
    assert pool.rows[row.id] == occupied


async def test_recovered_start_counts_once_even_when_repeated(pool: _Pool) -> None:
    row = _schedule()
    pool.rows[row.id] = row
    (first,) = await agent_schedule_dao.claim_due(NOW, _next_run)
    assert first.lease_token is not None
    started_at = NOW + timedelta(seconds=1)
    assert await agent_schedule_dao.start_occurrence(row.id, first.lease_token, started_at)
    (recovered,) = await agent_schedule_dao.claim_due(NOW + timedelta(minutes=5), _next_run)
    assert recovered.lease_token is not None
    outcomes = await asyncio.gather(
        *(
            agent_schedule_dao.start_occurrence(row.id, recovered.lease_token, NOW + timedelta(minutes=6))
            for _ in range(2)
        )
    )
    assert outcomes == [True, True]
    assert pool.rows[row.id].run_count == row.run_count + 1
    assert pool.rows[row.id].last_run_at == started_at
    assert pool.rows[row.id].pending_started_at == started_at


@pytest.mark.parametrize("operation", ["renew_occurrence", "start_occurrence", "finish_occurrence"])
@pytest.mark.parametrize("state", ["expired", "reclaimed", "wrong_token", "disabled", "deleted"])
async def test_owner_writes_are_fenced(pool: _Pool, operation: str, state: str) -> None:
    row = _schedule()
    pool.rows[row.id] = row
    (claim,) = await agent_schedule_dao.claim_due(NOW, _next_run)
    assert claim.lease_token is not None
    token = claim.lease_token
    now = NOW
    if state == "expired":
        now += timedelta(minutes=5)
    elif state == "reclaimed":
        now += timedelta(minutes=5)
        await agent_schedule_dao.claim_due(now, _next_run)
    elif state == "wrong_token":
        token = uuid4()
    elif state == "disabled":
        await agent_schedule_dao.update(db_obj=row, obj_in={"is_enabled": False})
    else:
        del pool.rows[row.id]
    before = dict(pool.rows)
    write = {
        "renew_occurrence": agent_schedule_dao.renew_occurrence,
        "start_occurrence": agent_schedule_dao.start_occurrence,
        "finish_occurrence": agent_schedule_dao.finish_occurrence,
    }[operation]
    outcome = await write(row.id, token, now)
    assert outcome is False
    assert pool.rows == before


async def test_renew_extends_from_supplied_clock_and_defers_recovery(pool: _Pool) -> None:
    row = _schedule()
    pool.rows[row.id] = row
    (claim,) = await agent_schedule_dao.claim_due(NOW, _next_run)
    assert claim.lease_token is not None
    assert await agent_schedule_dao.renew_occurrence(row.id, claim.lease_token, NOW + timedelta(minutes=4))
    assert pool.rows[row.id] == replace(claim, lease_expires_at=NOW + timedelta(minutes=9))
    assert await agent_schedule_dao.claim_due(NOW + timedelta(minutes=5), _next_run) == []


@pytest.mark.parametrize("started", [False, True])
async def test_finish_clears_only_occurrence_and_allows_next_slot(pool: _Pool, started: bool) -> None:
    row = _schedule()
    pool.rows[row.id] = row
    (claim,) = await agent_schedule_dao.claim_due(NOW, _next_run)
    assert claim.lease_token is not None
    if started:
        assert await agent_schedule_dao.start_occurrence(row.id, claim.lease_token, NOW)
    assert await agent_schedule_dao.finish_occurrence(row.id, claim.lease_token, NOW)
    assert pool.rows[row.id] == replace(
        row, next_run_at=NEXT, run_count=row.run_count + int(started), last_run_at=NOW if started else None
    )
    assert not await agent_schedule_dao.finish_occurrence(row.id, claim.lease_token, NOW)
    (next_claim,) = await agent_schedule_dao.claim_due(NEXT, lambda *_: NEXT + timedelta(days=1))
    assert next_claim.pending_due_at == NEXT
    assert next_claim.lease_token != claim.lease_token
    assert next_claim.pending_started_at is None


async def test_claim_rolls_back_preceding_row_when_later_cron_raises(pool: _Pool) -> None:
    valid, broken = _schedule(), replace(_schedule(), cron_expr="raises")
    pool.rows = {row.id: row for row in (valid, broken)}

    def compute(cron: str, *_: object) -> datetime:
        if cron == "raises":
            raise ValueError("invalid timezone")
        return NEXT

    with pytest.raises(ValueError, match="invalid timezone"):
        await agent_schedule_dao.claim_due(NOW, compute)
    assert pool.rows == {row.id: row for row in (valid, broken)}
    assert pool.connections[0].commits == 0
    assert not pool.locked


async def test_claim_rolls_back_when_commit_fails(pool: _Pool, monkeypatch: pytest.MonkeyPatch) -> None:
    row = _schedule()
    pool.rows[row.id] = row
    monkeypatch.setattr(_Raw, "commit", AsyncMock(side_effect=RuntimeError("commit failed")))
    with pytest.raises(RuntimeError, match="commit failed"):
        await agent_schedule_dao.claim_due(NOW, _next_run)
    assert pool.rows[row.id] == row
    assert not pool.locked


async def test_start_and_finish_rollback_with_outer_transaction(pool: _Pool) -> None:
    row = _schedule()
    pool.rows[row.id] = row
    (claim,) = await agent_schedule_dao.claim_due(NOW, _next_run)
    assert claim.lease_token is not None

    async def fail_transaction() -> None:
        assert claim.lease_token is not None
        async with connection_ctx():
            assert await agent_schedule_dao.start_occurrence(row.id, claim.lease_token, NOW)
            assert await agent_schedule_dao.finish_occurrence(row.id, claim.lease_token, NOW)
            raise RuntimeError("activity failed")

    with pytest.raises(RuntimeError, match="activity failed"):
        await fail_transaction()
    assert pool.rows[row.id] == claim


def test_record_restores_nullable_occurrence_fields_and_empty_instruction() -> None:
    row = replace(
        _schedule(),
        ends_at=NEXT,
        pending_due_at=NOW,
        pending_instruction="",
        lease_token=uuid4(),
        lease_expires_at=NEXT,
        pending_started_at=NOW,
    )
    values = asdict(row)
    values["lease_token"] = str(row.lease_token)
    assert AgentScheduleRecord.from_row(values) == row
    empty = asdict(_schedule())
    assert all(empty[column] is None for column in SLOT_COLUMNS)
    assert AgentScheduleRecord.from_row(empty).pending_instruction is None
