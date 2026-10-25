"""Schedule SQL contracts exercised through real connection/session wrappers, without Postgres."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from app.dao.schedule_dao import agent_schedule_dao
from app.db import session as session_module
from app.db.pool import reset_pool_for_tests
from app.records.schedule import AgentScheduleRecord
from schedule_occurrence_persistence_fakes import NEXT, NOW, _Pool, _Raw

__all__ = ["NEXT", "NOW", "_Pool", "_Raw"]


@pytest.fixture
def pool(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Pool]:
    token = session_module._conn_ctx.set(None)
    reset_pool_for_tests()
    fake = _Pool()
    monkeypatch.setattr(session_module, "get_pool", lambda: fake)
    yield fake
    session_module._conn_ctx.reset(token)
    reset_pool_for_tests()


def _schedule() -> AgentScheduleRecord:
    return AgentScheduleRecord(
        id=uuid4(),
        agent_id=uuid4(),
        name="Daily",
        instruction="Report",
        cron_expr="0 12 * * *",
        created_by=uuid4(),
        next_run_at=NOW - timedelta(days=5),
        run_count=7,
    )


def _next_run(cron_expr: str, after: datetime, timezone: str = "UTC") -> datetime:
    assert cron_expr == "0 12 * * *"
    assert after == NOW
    assert timezone == "UTC"
    return NEXT


async def test_claim_coalesces_downtime_without_counting_when_due(pool: _Pool) -> None:
    # Given a schedule overdue by several occurrences.
    row = _schedule()
    pool.rows[row.id] = row
    # When claiming twice at the same instant.
    claims = await agent_schedule_dao.claim_due(NOW, _next_run)
    repeated = await agent_schedule_dao.claim_due(NOW, _next_run)
    # Then the first claim commits its future slot without recording a started run.
    assert len(claims) == 1
    claim = claims[0]
    assert claim == replace(
        row,
        next_run_at=NEXT,
        pending_due_at=row.next_run_at,
        pending_instruction=row.instruction,
        lease_token=claim.lease_token,
        lease_expires_at=NOW + timedelta(minutes=5),
    )
    assert claim.lease_token is not None
    assert repeated == []
    assert pool.rows[row.id] == claim
    assert [raw.commits for raw in pool.connections] == [1, 1]


async def test_claim_skips_locked_occurrence_when_ticks_interleave(pool: _Pool) -> None:
    # Given two connections contending while the first SELECT holds a row lock.
    row = _schedule()
    pool.rows[row.id] = row
    pool.pause_first = True
    async with asyncio.TaskGroup() as tasks:
        first = tasks.create_task(agent_schedule_dao.claim_due(NOW, _next_run))
        await asyncio.wait_for(pool.selected.wait(), timeout=2)
        # When the second tick claims before the first can update or commit.
        try:
            second = await agent_schedule_dao.claim_due(NOW, _next_run)
        finally:
            pool.release.set()
    # Then exactly one tick owns the occurrence and both transactions commit.
    assert [claim.id for claim in first.result()] == [row.id]
    assert second == []
    assert [raw.commits for raw in pool.connections] == [1, 1]


@pytest.mark.parametrize("state", ["disabled", "unscheduled", "future"])
async def test_claim_excludes_ineligible_rows_when_present(pool: _Pool, state: str) -> None:
    # Given a row that is disabled, lacks a slot, or is not due yet.
    row = _schedule()
    pool.rows[row.id] = replace(
        row,
        is_enabled=state != "disabled",
        next_run_at=None if state == "unscheduled" else row.next_run_at if state == "disabled" else NEXT,
    )
    # When claiming due schedules.
    claims = await agent_schedule_dao.claim_due(NOW, _next_run)
    # Then no update or execution claim is produced.
    assert claims == []
    assert len(pool.connections[0].executed) == 1


async def test_claim_disables_invalid_cron_without_blocking_valid_rows(
    pool: _Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an invalid cron beside a valid due schedule.
    invalid, valid = replace(_schedule(), cron_expr="invalid"), _schedule()
    pool.rows = {row.id: row for row in (invalid, valid)}
    warnings: list[tuple[str, Mapping[str, object]]] = []

    class _Logger:
        def bind(self, **fields: object) -> _Logger:
            assert fields == {"schedule_id": str(invalid.id)}
            return self

        def warning(self, message: str) -> None:
            warnings.append((message, {"schedule_id": str(invalid.id)}))

    monkeypatch.setattr("app.dao.schedule_dao.logger", _Logger())
    # When the callback reports an unparseable cron via None.
    claims = await agent_schedule_dao.claim_due(
        NOW, lambda cron, after, timezone: None if cron == "invalid" else _next_run(cron, after, timezone)
    )
    # Then the broken schedule is disabled, logged, and excluded without a count update.
    assert len(claims) == 1
    assert claims[0].id == valid.id
    assert claims[0].pending_due_at == valid.next_run_at
    assert claims[0].next_run_at == NEXT
    assert pool.rows[invalid.id] == replace(invalid, is_enabled=False, next_run_at=None)
    assert len(warnings) == 1


@pytest.mark.parametrize("count", [None, 0, 7])
async def test_record_started_increments_atomically_without_advancing_slot(pool: _Pool, count: int | None) -> None:
    # Given a persisted count, including legacy NULL (decoded as zero by from_row).
    row = _schedule()
    pool.rows[row.id] = replace(row, run_count=count or 0)
    pool.counts[row.id] = count
    # When two executions start without reading the row first.
    await asyncio.gather(*(agent_schedule_dao.record_started(row.id, NOW) for _ in range(2)))
    # Then both increments survive and only the start bookkeeping changes.
    assert pool.rows[row.id] == replace(row, run_count=(count or 0) + 2, last_run_at=NOW)
    assert pool.counts[row.id] == (count or 0) + 2
    assert all(len(raw.executed) == 1 and raw.commits == 1 for raw in pool.connections)
