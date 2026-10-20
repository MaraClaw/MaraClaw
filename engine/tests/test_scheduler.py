import asyncio
from dataclasses import replace
from datetime import datetime, tzinfo
from typing import Self, override
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.logging import get_trace_id, set_trace_id
from app.dao.agent_dao import agent_dao
from app.db import session
from app.records.agent import AgentRecord
from app.records.schedule import AgentScheduleRecord
from app.services import scheduler, scheduler_tasks
from test_schedule_dao import NEXT, NOW, _next_run, _Pool, _Raw, _schedule, pool as pool
from test_scheduler_tasks import owner as owner


class _Clock(datetime):
    @classmethod
    @override
    def now(cls, tz: tzinfo | None = None) -> Self:
        return cls.fromtimestamp(NOW.timestamp(), tz)


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduler, "datetime", _Clock)


async def test_tick_commits_claims_before_detached_launch_and_coalesces_downtime(
    pool: _Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given two overdue schedules and a caller trace.
    rows = [_schedule(), replace(_schedule(), instruction="Other report")]
    pool.rows = {row.id: row for row in rows}
    monkeypatch.setattr(scheduler, "compute_next_run", _next_run)
    set_trace_id("caller")
    completed = asyncio.Event()
    launches: list[AgentScheduleRecord] = []
    tick_traces: list[str | None] = []

    async def execute(claim: AgentScheduleRecord) -> None:
        assert session.get_connection() is None
        assert session._completion_ctx.get() is None
        assert get_trace_id() is None
        assert pool.connections[0].commits == 1
        launches.append(claim)
        if len(launches) == len(rows):
            completed.set()

    async def audit(*_args: object, **_kwargs: object) -> None:
        assert session.get_connection() is None
        assert pool.connections[0].commits == 1
        tick_traces.append(get_trace_id())

    monkeypatch.setattr(scheduler, "_execute_periodic_schedule", execute)
    monkeypatch.setattr("app.services.audit_logger.write_audit_log", audit)
    # When two ticks observe the same overdue occurrences.
    await scheduler._tick()
    first_trace = get_trace_id()
    await scheduler._tick()
    await asyncio.wait_for(completed.wait(), timeout=1)
    # Then each bound row launches once, with slots advanced but counts unchanged.
    assert {claim.id for claim in launches} == {row.id for row in rows}
    assert all(pool.rows[claim.id] == claim for claim in launches)
    assert all(claim.next_run_at == NEXT and claim.run_count == 7 for claim in launches)
    assert all(claim.pending_due_at == row.next_run_at for claim, row in zip(launches, rows, strict=True))
    assert all(claim.pending_instruction == row.instruction for claim, row in zip(launches, rows, strict=True))
    assert tick_traces == [first_trace] * 3
    assert first_trace is not None
    assert first_trace != "caller"
    assert get_trace_id() not in {"caller", first_trace, None}


async def test_tick_drops_registered_factories_when_commit_fails(
    pool: _Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a due claim whose transaction cannot commit.
    row = _schedule()
    pool.rows[row.id] = row
    monkeypatch.setattr(scheduler, "compute_next_run", _next_run)
    commit = AsyncMock(side_effect=RuntimeError("commit failed"))
    monkeypatch.setattr(_Raw, "commit", commit)
    execute = Mock(side_effect=AssertionError("launched on rollback"))
    monkeypatch.setattr(scheduler, "_execute_periodic_schedule", execute)
    audit = AsyncMock()
    monkeypatch.setattr("app.services.audit_logger.write_audit_log", audit)
    # When the tick attempts to commit.
    await scheduler._tick()
    # Then rollback discards execution admission and reports the failure.
    commit.assert_awaited_once()
    execute.assert_not_called()
    assert not scheduler_tasks._tasks
    assert not pool.locked
    assert audit.await_args_list[0].args[0] == "schedule_error"


@pytest.mark.parametrize("state", ["missing", "stopped", "expired"])
async def test_execution_skips_without_counting_when_agent_ineligible(
    pool: _Pool,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    # Given a persisted schedule and an agent no longer eligible at execution time.
    row = _schedule()
    pool.rows[row.id] = row
    agent = AgentRecord(id=row.agent_id, name="Agent", creator_id=row.created_by, status="running")
    agent.status = "stopped" if state == "stopped" else "running"
    agent.is_expired = state == "expired"
    monkeypatch.setattr(agent_dao, "get", AsyncMock(return_value=None if state == "missing" else agent))
    context = AsyncMock(side_effect=AssertionError("built ineligible context"))
    monkeypatch.setattr("app.services.agent_context.build_agent_context", context)
    activity = AsyncMock()
    monkeypatch.setattr("app.services.activity_logger.log_activity", activity)
    set_trace_id("request")
    # When execution reloads the agent.
    await scheduler._execute_schedule(row.id, row.agent_id, row.instruction)
    # Then no bookkeeping or LLM context is started, and the skip has its own trace.
    assert pool.rows[row.id] == row
    assert pool.connections == []
    context.assert_not_awaited()
    activity.assert_not_awaited()
    assert get_trace_id() not in {None, "request"}


@pytest.mark.parametrize("failure", [None, "provider", "context"])
@pytest.mark.parametrize("enabled", [False, True])
async def test_execution_counts_started_attempt_before_detached_llm_and_retains_history(
    pool: _Pool,
    monkeypatch: pytest.MonkeyPatch,
    failure: str | None,
    enabled: bool,
) -> None:
    # Given a running agent, with real atomic bookkeeping and an external LLM seam.
    row = replace(_schedule(), is_enabled=enabled)
    pool.rows[row.id] = row
    agent = AgentRecord(id=row.agent_id, name="Agent", creator_id=row.created_by, status="running")
    monkeypatch.setattr(agent_dao, "get", AsyncMock(return_value=agent))
    traces: list[str | None] = []

    async def context(*_args: object) -> tuple[str, str]:
        assert session.get_connection() is None
        assert pool.rows[row.id].run_count == row.run_count + 1
        assert pool.connections[0].commits == 1
        traces.append(get_trace_id())
        if failure == "context":
            raise RuntimeError("context failure")
        return "static", "dynamic"

    async def llm(**kwargs: object) -> str:
        assert session.get_connection() is None
        assert kwargs["agent_id"] == row.agent_id
        assert kwargs["session_id"] == str(row.id)
        traces.append(get_trace_id())
        if failure == "provider":
            raise RuntimeError("provider failure")
        return "R" * 600

    monkeypatch.setattr("app.services.agent_context.build_agent_context", context)
    monkeypatch.setattr("app.services.llm.call_agent_llm_with_tools", llm)
    activity = AsyncMock()
    monkeypatch.setattr("app.services.activity_logger.log_activity", activity)
    set_trace_id("request")
    # When the eligible execution succeeds or context/provider work fails.
    await scheduler._execute_schedule(row.id, row.agent_id, row.instruction)
    # Then either result counts one committed attempt without changing the cron slot.
    assert pool.rows[row.id] == replace(row, run_count=row.run_count + 1, last_run_at=NOW)
    assert traces[0] is not None
    assert traces[0] != "request"
    assert traces == [traces[0]] * (1 if failure == "context" else 2)
    activity.assert_awaited_once()
    assert activity.await_args_list[0].args[:2] == (row.agent_id, "schedule_run")
    if failure is not None:
        assert activity.await_args_list[0].args[2] == f"Scheduled task failed: {row.instruction[:60]}"
        assert activity.await_args_list[0].kwargs["detail"] == {
            "schedule_id": str(row.id),
            "instruction": row.instruction,
            "status": "failure",
        }
    else:
        assert activity.await_args_list[0].args[2] == f"Scheduled task executed: {row.instruction[:60]}"
        assert activity.await_args_list[0].kwargs["detail"] == {
            "schedule_id": str(row.id),
            "instruction": row.instruction,
            "reply": "R" * 500,
            "status": "success",
        }


@pytest.mark.parametrize("stage", ["context", "provider"])
async def test_execution_retains_cancelled_history_when_owner_stops(
    pool: _Pool,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    # Given an owned execution blocked in its provider after the start committed.
    row = _schedule()
    pool.rows[row.id] = row
    agent = AgentRecord(id=row.agent_id, name="Agent", creator_id=row.created_by, status="running")
    monkeypatch.setattr(agent_dao, "get", AsyncMock(return_value=agent))
    started = asyncio.Event()

    async def context(*_args: object) -> tuple[str, str]:
        if stage == "context":
            started.set()
            await asyncio.Event().wait()
        return "static", "dynamic"

    async def llm(**_kwargs: object) -> str:
        assert session.get_connection() is None
        assert pool.connections[0].commits == 1
        started.set()
        await asyncio.Event().wait()
        return "unreachable"

    monkeypatch.setattr("app.services.agent_context.build_agent_context", context)
    monkeypatch.setattr("app.services.llm.call_agent_llm_with_tools", llm)
    activity = AsyncMock()
    monkeypatch.setattr("app.services.activity_logger.log_activity", activity)
    scheduler_tasks.schedule_execution(lambda: scheduler._execute_schedule(row.id, row.agent_id, row.instruction))
    await asyncio.wait_for(started.wait(), timeout=1)
    (execution,) = scheduler_tasks._tasks
    # When shutdown cancels and awaits the owned attempt.
    await scheduler_tasks.stop_schedule_tasks()
    # Then cancellation propagates, with one count and one honest history entry.
    assert execution.cancelled()
    assert not scheduler_tasks._tasks
    assert pool.rows[row.id] == replace(row, run_count=row.run_count + 1, last_run_at=NOW)
    activity.assert_awaited_once_with(
        row.agent_id,
        "schedule_run",
        f"Scheduled task cancelled: {row.instruction[:60]}",
        detail={"schedule_id": str(row.id), "instruction": row.instruction, "status": "cancelled"},
    )


async def test_tick_launches_once_after_commit_when_competing_ticks_interleave(
    pool: _Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given the first real tick paused while holding the due occurrence's lock.
    row = _schedule()
    pool.rows[row.id] = row
    pool.pause_first = True
    monkeypatch.setattr(scheduler, "compute_next_run", _next_run)
    monkeypatch.setattr("app.services.audit_logger.write_audit_log", AsyncMock())
    completed = asyncio.Event()
    launches: list[AgentScheduleRecord] = []

    async def execute(claim: AgentScheduleRecord) -> None:
        assert session.get_connection() is None
        assert session._completion_ctx.get() is None
        assert get_trace_id() is None
        assert [raw.commits for raw in pool.connections] == [1, 1]
        launches.append(claim)
        completed.set()

    monkeypatch.setattr(scheduler, "_execute_periodic_schedule", execute)
    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(scheduler._tick())
        await asyncio.wait_for(pool.selected.wait(), timeout=1)
        # When another full tick runs before the lock owner updates or commits.
        try:
            await scheduler._tick()
            assert launches == []
            assert not scheduler_tasks._tasks
            assert [raw.commits for raw in pool.connections] == [0, 1]
        finally:
            pool.release.set()
    await asyncio.wait_for(completed.wait(), timeout=1)
    # Then the committed occurrence produces exactly one detached execution.
    assert len(launches) == 1
    assert launches[0].id == row.id
    assert launches[0].pending_instruction == row.instruction
    assert pool.rows[row.id] == launches[0]
    assert pool.rows[row.id].next_run_at == NEXT
