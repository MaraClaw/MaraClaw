"""Schedule SQL contracts exercised through real connection/session wrappers, without Postgres."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Iterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.dao.schedule_dao import agent_schedule_dao
from app.db import session as session_module
from app.db.pool import reset_pool_for_tests
from app.records.schedule import AgentScheduleRecord

NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)
NEXT = NOW + timedelta(days=1)


class _Cursor:
    def __init__(self, raw: _Raw) -> None:
        self.raw = raw
        self.rows: list[dict[str, object]] = []

    async def __aenter__(self) -> _Cursor:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def execute(self, query: str, params: Mapping[str, object]) -> None:
        sql = " ".join(query.split())
        self.raw.executed.append((sql, params))
        pool = self.raw.pool
        if sql.startswith("SELECT"):
            assert "WHERE is_enabled IS TRUE AND next_run_at <= %(now)s FOR UPDATE SKIP LOCKED" in sql
            assert params == {"now": NOW}
            self.rows = []
            for row in pool.rows.values():
                if (
                    row.is_enabled
                    and row.next_run_at is not None
                    and row.next_run_at <= NOW
                    and row.id not in pool.locked
                ):
                    pool.locked.add(row.id)
                    self.raw.locked.add(row.id)
                    self.rows.append(asdict(row))
            if pool.pause_first and len(pool.connections) == 1:
                pool.selected.set()
                await pool.release.wait()
            return
        schedule_id = params["id"]
        assert isinstance(schedule_id, UUID)
        row = pool.rows[schedule_id]
        if "COALESCE" in sql:
            assert sql == (
                "UPDATE agent_schedules SET run_count = COALESCE(run_count, 0) + 1, last_run_at = %(now)s "
                "WHERE id = %(id)s"
            )
            assert params == {"id": schedule_id, "now": NOW}
            count = (pool.counts.get(schedule_id, row.run_count) or 0) + 1
            pool.counts[schedule_id] = count
            pool.rows[schedule_id] = replace(row, run_count=count, last_run_at=NOW)
        elif "is_enabled = FALSE" in sql:
            assert sql == "UPDATE agent_schedules SET is_enabled = FALSE, next_run_at = NULL WHERE id = %(id)s"
            assert params == {"id": schedule_id}
            assert schedule_id in self.raw.locked
            pool.rows[schedule_id] = replace(row, is_enabled=False, next_run_at=None)
        else:
            assert sql == "UPDATE agent_schedules SET next_run_at = %(next_run_at)s WHERE id = %(id)s"
            assert params == {"id": schedule_id, "next_run_at": NEXT}
            assert schedule_id in self.raw.locked
            pool.rows[schedule_id] = replace(row, next_run_at=NEXT)

    async def fetchall(self) -> list[dict[str, object]]:
        return self.rows


class _Raw:
    """Mutable transaction state; locks are released only by commit or rollback."""

    def __init__(self, pool: _Pool) -> None:
        self.pool = pool
        self.locked: set[UUID] = set()
        self.executed: list[tuple[str, Mapping[str, object]]] = []
        self.commits = 0

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    async def commit(self) -> None:
        self.commits += 1
        self.pool.locked.difference_update(self.locked)

    async def rollback(self) -> None:
        self.pool.locked.difference_update(self.locked)


class _Pool:
    """In-memory rows and lock ownership shared by competing fake raw connections."""

    def __init__(self) -> None:
        self.rows: dict[UUID, AgentScheduleRecord] = {}
        self.counts: dict[UUID, int | None] = {}
        self.locked: set[UUID] = set()
        self.connections: list[_Raw] = []
        self.pause_first = False
        self.selected = asyncio.Event()
        self.release = asyncio.Event()

    @asynccontextmanager
    async def connection(self) -> AsyncGenerator[_Raw]:
        raw = _Raw(self)
        self.connections.append(raw)
        yield raw


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


def _next_run(cron_expr: str, after: datetime) -> datetime:
    assert cron_expr == "0 12 * * *"
    assert after == NOW
    return NEXT


async def test_claim_coalesces_downtime_without_counting_when_due(pool: _Pool) -> None:
    # Given a schedule overdue by several occurrences.
    row = _schedule()
    pool.rows[row.id] = row
    # When claiming twice at the same instant.
    claims = await agent_schedule_dao.claim_due(NOW, _next_run)
    repeated = await agent_schedule_dao.claim_due(NOW, _next_run)
    # Then the first claim commits its future slot without recording a started run.
    assert claims == [replace(row, next_run_at=NEXT)]
    assert repeated == []
    assert pool.rows[row.id] == replace(row, next_run_at=NEXT)
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
        NOW, lambda cron, after: None if cron == "invalid" else _next_run(cron, after)
    )
    # Then the broken schedule is disabled, logged, and excluded without a count update.
    assert claims == [replace(valid, next_run_at=NEXT)]
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
