import asyncio
from dataclasses import asdict, replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest

from app.dao.schedule_dao import agent_schedule_dao
from app.db.connection import DbConnection
from app.services import scheduler, scheduler_tasks
from app.services.schedule_time import ScheduleTimezoneError
from test_schedule_dao import NOW, _Pool, _schedule, pool as pool
from test_scheduler import clock as clock
from test_scheduler_tasks import owner as owner


@pytest.mark.parametrize("zone", [None, "Asia/Ho_Chi_Minh"])
async def test_create_writes_timezone_and_decodes_returned_row(
    pool: _Pool, monkeypatch: pytest.MonkeyPatch, zone: str | None
) -> None:
    row = replace(_schedule(), timezone=zone or "UTC")
    fetchone = AsyncMock(return_value=asdict(row))
    monkeypatch.setattr(DbConnection, "fetchone", fetchone)
    values: dict[str, object] = {
        "agent_id": row.agent_id,
        "name": row.name,
        "instruction": row.instruction,
        "cron_expr": row.cron_expr,
        "created_by": row.created_by,
    }
    if zone is not None:
        values["timezone"] = zone
    result = await agent_schedule_dao.create(obj_in=values)
    sql, params = fetchone.await_args_list[0].args
    assert sql.startswith("INSERT INTO agent_schedules (")
    assert "%(timezone)s" in sql
    assert "timezone" in sql.split("RETURNING", 1)[1]
    assert params["timezone"] == (zone or "UTC")
    assert result == row
    assert pool.connections[0].commits == 1


async def test_list_restores_persisted_timezone(pool: _Pool, monkeypatch: pytest.MonkeyPatch) -> None:
    row = replace(_schedule(), timezone="Australia/Lord_Howe")
    fetchall = AsyncMock(return_value=[asdict(row)])
    monkeypatch.setattr(DbConnection, "fetchall", fetchall)
    result = await agent_schedule_dao.list_for_agent(row.agent_id)
    sql, params = fetchall.await_args_list[0].args
    assert "timezone" in sql.split("FROM", 1)[0]
    assert params == {"agent_id": row.agent_id}
    assert result == [row]


async def test_update_writes_timezone_and_decodes_returned_row(pool: _Pool, monkeypatch: pytest.MonkeyPatch) -> None:
    row = _schedule()
    updated = replace(row, timezone="America/New_York")
    fetchone = AsyncMock(return_value=asdict(updated))
    monkeypatch.setattr(DbConnection, "fetchone", fetchone)
    result = await agent_schedule_dao.update(db_obj=row, obj_in={"timezone": updated.timezone})
    sql, params = fetchone.await_args_list[0].args
    assert sql.startswith("UPDATE agent_schedules SET timezone = %(timezone)s WHERE id = %(id)s RETURNING")
    assert params == {"id": row.id, "timezone": updated.timezone}
    assert result == updated


async def test_claim_advances_in_saved_timezone_without_replaying_missed_runs(pool: _Pool) -> None:
    row = replace(_schedule(), timezone="Asia/Ho_Chi_Minh")
    pool.rows[row.id] = row
    pool.next_run = datetime(2026, 10, 16, 5, tzinfo=UTC)
    claims = await agent_schedule_dao.claim_due(NOW, scheduler.compute_next_run)
    assert claims == [replace(row, next_run_at=pool.next_run)]
    assert pool.rows[row.id].run_count == row.run_count
    assert pool.connections[0].commits == 1


async def test_claim_rejects_unknown_persisted_zone_without_advancing(pool: _Pool) -> None:
    row = replace(_schedule(), timezone="Not/A_Zone")
    pool.rows[row.id] = row
    with pytest.raises(ScheduleTimezoneError):
        await agent_schedule_dao.claim_due(NOW, scheduler.compute_next_run)
    assert pool.rows[row.id] == row
    assert pool.connections[0].commits == 0
    assert not pool.locked


async def test_tick_uses_saved_timezone_before_postcommit_dispatch(
    pool: _Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = replace(_schedule(), timezone="Asia/Ho_Chi_Minh")
    pool.rows[row.id] = row
    pool.next_run = datetime(2026, 10, 16, 5, tzinfo=UTC)
    completed = asyncio.Event()

    async def execute(schedule_id: UUID, agent_id: UUID, instruction: str) -> None:
        assert (schedule_id, agent_id, instruction) == (row.id, row.agent_id, row.instruction)
        assert pool.rows[row.id] == replace(row, next_run_at=pool.next_run)
        assert pool.connections[0].commits == 1
        completed.set()

    monkeypatch.setattr(scheduler, "_execute_schedule", execute)
    monkeypatch.setattr("app.services.audit_logger.write_audit_log", AsyncMock())
    await scheduler._tick()
    await asyncio.wait_for(completed.wait(), timeout=1)
    assert pool.rows[row.id].run_count == row.run_count


async def test_tick_reports_unknown_zone_without_dispatch_or_utc_fallback(
    pool: _Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = replace(_schedule(), timezone="Not/A_Zone")
    pool.rows[row.id] = row
    execute = Mock()
    audit = AsyncMock()
    monkeypatch.setattr(scheduler, "_execute_schedule", execute)
    monkeypatch.setattr("app.services.audit_logger.write_audit_log", audit)
    await scheduler._tick()
    execute.assert_not_called()
    assert not scheduler_tasks._tasks
    assert pool.rows[row.id] == row
    assert pool.connections[0].commits == 0
    assert audit.await_args_list[0].args[0] == "schedule_error"
    assert "timezone" in audit.await_args_list[0].args[1]["error"]
