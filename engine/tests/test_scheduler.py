import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, tzinfo
from typing import Self, override
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.config import get_settings
from app.core.domain.agents import AgentStatus
from app.core.logging import get_trace_id, set_trace_id
from app.dao.agent_dao import agent_dao
from app.dao.schedule_manual_run_dao import schedule_manual_run_dao
from app.db import session
from app.records.agent import AgentRecord
from app.records.schedule import AgentScheduleRecord
from app.records.schedule_manual_run import ScheduleManualRunRecord
from app.services import schedule_manual, scheduler, scheduler_tasks
from test_schedule_dao import NEXT, NOW, _next_run, _Pool, _Raw, _schedule, pool
from test_scheduler_tasks import owner
from worker_progress_fixtures import WorkerHTTP, worker_http

__all__ = ["owner", "pool", "worker_http"]


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
    by_id = {row.id: row for row in rows}
    assert all(claim.pending_due_at == by_id[claim.id].next_run_at for claim in launches)
    assert all(claim.pending_instruction == by_id[claim.id].instruction for claim in launches)
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
    agent = AgentRecord(id=row.agent_id, name="Agent", creator_id=row.created_by, status=AgentStatus.RUNNING)
    agent.status = AgentStatus.STOPPED if state == "stopped" else AgentStatus.RUNNING
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
    agent = AgentRecord(id=row.agent_id, name="Agent", creator_id=row.created_by, status=AgentStatus.RUNNING)
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
    agent = AgentRecord(id=row.agent_id, name="Agent", creator_id=row.created_by, status=AgentStatus.RUNNING)
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


def _cap(monkeypatch: pytest.MonkeyPatch, cap: int) -> None:
    monkeypatch.setattr(get_settings(), "SCHEDULE_MAX_CONCURRENT_EXECUTIONS", cap)
    monkeypatch.setattr("app.services.audit_logger.write_audit_log", AsyncMock())


async def test_tick_claims_only_free_capacity_and_leaves_the_rest_for_peers(
    pool: _Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given five due schedules, a cap of two and executions that stay running.
    rows = [replace(_schedule(), next_run_at=NOW - timedelta(hours=hour + 1)) for hour in range(5)]
    pool.rows = {row.id: row for row in rows}
    _cap(monkeypatch, 2)
    monkeypatch.setattr(scheduler, "compute_next_run", _next_run)
    gate = asyncio.Event()

    async def execute(_claim: AgentScheduleRecord) -> None:
        await gate.wait()

    monkeypatch.setattr(scheduler, "_execute_periodic_schedule", execute)
    # When two consecutive ticks run.
    await scheduler._tick()
    await scheduler._tick()
    # Then the two oldest are leased and running; the second tick saw no capacity and touched nothing.
    leased = {row.id for row in pool.rows.values() if row.lease_token is not None}
    assert leased == {rows[4].id, rows[3].id}
    assert len(scheduler_tasks._tasks) == 2
    assert len(pool.connections) == 1
    gate.set()


async def test_tick_does_not_claim_when_worker_is_not_accepting(
    pool: _Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _schedule()
    pool.rows[row.id] = row
    _cap(monkeypatch, 2)
    await scheduler_tasks.stop_schedule_tasks()
    await scheduler._tick()
    assert pool.connections == []
    assert pool.rows[row.id] == row


async def test_tick_releases_claims_that_never_launched_and_a_peer_reclaims_them(
    pool: _Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a claim whose launch is refused after commit (for example a stop racing the tick).
    row = _schedule()
    pool.rows[row.id] = row
    _cap(monkeypatch, 2)
    monkeypatch.setattr(scheduler, "compute_next_run", _next_run)

    def refuse(_factory: object) -> asyncio.Future[bool]:
        refused = asyncio.Future[bool]()
        refused.set_result(False)
        return refused

    with monkeypatch.context() as refusing:
        refusing.setattr(scheduler, "schedule_execution", refuse)
        # When the tick finishes.
        await scheduler._tick()
    # Then the lease expires now while the occurrence and next slot stay intact.
    released = pool.rows[row.id]
    assert released.lease_expires_at == NOW
    assert released.lease_token is not None
    assert released.pending_due_at == row.next_run_at
    assert released.pending_instruction == row.instruction
    assert released.next_run_at == NEXT
    # And another worker's next tick takes the same occurrence without a new cron computation.
    monkeypatch.setattr(scheduler, "compute_next_run", Mock(side_effect=AssertionError("recomputed")))
    launched: list[AgentScheduleRecord] = []

    async def execute(claim: AgentScheduleRecord) -> None:
        launched.append(claim)

    monkeypatch.setattr(scheduler, "_execute_periodic_schedule", execute)
    await scheduler._tick()
    await asyncio.gather(*scheduler_tasks._tasks)
    assert [claim.pending_due_at for claim in launched] == [row.next_run_at]
    assert launched[0].lease_token != released.lease_token


def _manual_queue(monkeypatch: pytest.MonkeyPatch, count: int) -> list[str]:
    """Fake the manual-run table: a FIFO queue handing out at most ``limit`` runs per claim."""
    pending = [
        ScheduleManualRunRecord(
            id=uuid4(), schedule_id=uuid4(), agent_id=uuid4(), requester_id=uuid4(), instruction="Run"
        )
        for _ in range(count)
    ]
    events: list[str] = []

    async def claim_pending(_now: datetime, limit: int = 100) -> list[ScheduleManualRunRecord]:
        taken, pending[:] = pending[:limit], pending[limit:]
        return [replace(run, lease_token=uuid4()) for run in taken]

    async def execute(_run: ScheduleManualRunRecord) -> None:
        events.append("manual")

    monkeypatch.setattr(schedule_manual_run_dao, "claim_pending", claim_pending)
    monkeypatch.setattr(schedule_manual, "execute_manual_run", execute)
    return events


@pytest.mark.parametrize("cap", [1, 2])
async def test_dispatch_rounds_alternate_manual_and_periodic_when_both_are_backlogged(
    pool: _Pool,
    monkeypatch: pytest.MonkeyPatch,
    cap: int,
) -> None:
    # Given a deep backlog of both classes and a cap too small to serve everything.
    pool.rows = {
        row.id: row for row in (replace(_schedule(), next_run_at=NOW - timedelta(minutes=i)) for i in range(9))
    }
    _cap(monkeypatch, cap)
    monkeypatch.setattr(scheduler, "_manual_first", True)
    monkeypatch.setattr(scheduler, "compute_next_run", _next_run)
    events = _manual_queue(monkeypatch, 9)

    async def execute(_claim: AgentScheduleRecord) -> None:
        events.append("periodic")

    monkeypatch.setattr(scheduler, "_execute_periodic_schedule", execute)
    # When consecutive rounds run to completion.
    for _ in range(3):
        await scheduler._dispatch_round()
        await asyncio.gather(*scheduler_tasks._tasks)
    # Then neither class is starved: every round serves both (cap 2) or hands the slot over (cap 1).
    if cap == 1:
        assert events == ["manual", "periodic", "manual"]
    else:
        assert sorted(events) == ["manual"] * 3 + ["periodic"] * 3
        assert events[:2] == ["manual", "periodic"]


async def test_dispatch_round_gives_the_whole_budget_to_the_class_that_has_work(
    pool: _Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given only periodic work and a cap of three.
    pool.rows = {
        row.id: row for row in (replace(_schedule(), next_run_at=NOW - timedelta(minutes=i)) for i in range(5))
    }
    _cap(monkeypatch, 3)
    monkeypatch.setattr(scheduler, "_manual_first", True)
    monkeypatch.setattr(scheduler, "compute_next_run", _next_run)
    _ = _manual_queue(monkeypatch, 0)
    gate = asyncio.Event()

    async def execute(_claim: AgentScheduleRecord) -> None:
        await gate.wait()

    monkeypatch.setattr(scheduler, "_execute_periodic_schedule", execute)
    # When a round runs.
    await scheduler._dispatch_round()
    # Then manual's idle share is not wasted.
    assert len(scheduler_tasks._tasks) == 3
    gate.set()


async def test_manual_recovery_releases_runs_that_never_launched(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a claimed run whose launch is refused after commit.
    _cap(monkeypatch, 2)
    run = ScheduleManualRunRecord(
        id=uuid4(), schedule_id=uuid4(), agent_id=uuid4(), requester_id=uuid4(), instruction="Run", lease_token=uuid4()
    )
    monkeypatch.setattr(schedule_manual_run_dao, "claim_pending", AsyncMock(return_value=[run]))
    release = AsyncMock(return_value=True)
    monkeypatch.setattr(schedule_manual_run_dao, "release_unstarted", release)
    monkeypatch.setattr(schedule_manual, "schedule_execution", lambda _factory: _resolved(False))
    monkeypatch.setattr(schedule_manual, "connection_ctx", _NoDb)
    # When recovery cannot launch it.
    await schedule_manual.recover_manual_runs()
    # Then its token is released.
    release.assert_awaited_once_with(run.id, run.lease_token)


def _resolved(value: bool) -> asyncio.Future[bool]:
    future = asyncio.Future[bool]()
    future.set_result(value)
    return future


class _NoDb:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


@pytest.mark.parametrize("capacity", [0, 2])
async def test_scheduler_idle_and_full_capacity_keep_http_ready(
    pool: _Pool, monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, capacity: int
) -> None:
    _cap(monkeypatch, max(1, capacity))
    if capacity == 0:
        entered = asyncio.Event()

        async def execution() -> None:
            entered.set()
            await asyncio.Event().wait()

        _ = scheduler_tasks.schedule_execution(execution)
        await asyncio.wait_for(entered.wait(), 2)
    executing = set(scheduler_tasks._tasks)
    monkeypatch.setattr(schedule_manual_run_dao, "claim_pending", AsyncMock(return_value=[]))
    monkeypatch.setattr(scheduler.asyncio, "sleep", worker_http.pacing.wait)
    worker_http.runtime.tasks.start(
        "scheduler", scheduler.start_scheduler(), requirement="loop", progress_budget_seconds=300
    )
    await worker_http.pacing.paused()
    for _ in range(4):
        await worker_http.pacing.step()
        await worker_http.ready(expected=True)
    assert pool.rows == {}
    assert scheduler_tasks._tasks == executing


@pytest.mark.parametrize("claim", ["manual", "periodic"])
async def test_scheduler_caught_claim_errors_cannot_be_masked_by_the_other_scanner(
    pool: _Pool, monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, claim: str
) -> None:
    from app.dao.schedule_dao import agent_schedule_dao

    _cap(monkeypatch, 2)
    manual, periodic = AsyncMock(return_value=[]), AsyncMock(return_value=[])
    monkeypatch.setattr(schedule_manual_run_dao, "claim_pending", manual)
    monkeypatch.setattr(agent_schedule_dao, "claim_due", periodic)
    monkeypatch.setattr(scheduler.asyncio, "sleep", worker_http.pacing.wait)
    worker_http.runtime.tasks.start(
        "scheduler", scheduler.start_scheduler(), requirement="loop", progress_budget_seconds=300
    )
    await worker_http.pacing.paused()
    failure = manual if claim == "manual" else periodic
    failure.side_effect = OSError("private claim failure")
    for _ in range(4):
        await worker_http.pacing.step()
    await worker_http.ready(expected=False)
    assert not worker_http.runtime.tasks.required["scheduler"][0].done()
    assert not scheduler_tasks._tasks
    assert pool.rows == {}
    failure.side_effect = None
    await worker_http.pacing.step()
    await worker_http.ready(expected=True)


@pytest.mark.parametrize("claim", ["manual", "periodic"])
async def test_scheduler_suspended_claim_recovers_http_readiness_without_advancing_schedule(
    pool: _Pool, monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, claim: str
) -> None:
    from app.dao.schedule_dao import agent_schedule_dao

    _cap(monkeypatch, 2)
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(*_args: object) -> list[object]:
        entered.set()
        await release.wait()
        return []

    monkeypatch.setattr(
        schedule_manual_run_dao, "claim_pending", blocked if claim == "manual" else AsyncMock(return_value=[])
    )
    monkeypatch.setattr(agent_schedule_dao, "claim_due", blocked if claim == "periodic" else AsyncMock(return_value=[]))
    monkeypatch.setattr(scheduler.asyncio, "sleep", worker_http.pacing.wait)
    worker_http.runtime.tasks.start(
        "scheduler", scheduler.start_scheduler(), requirement="loop", progress_budget_seconds=300
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        worker_http.pacing.now += 301
        await worker_http.ready(expected=False)
        assert pool.rows == {}
        assert not scheduler_tasks._tasks
        assert not worker_http.runtime.tasks.required["scheduler"][0].done()
    finally:
        release.set()
    await worker_http.pacing.paused()
    await worker_http.ready(expected=True)


async def test_scheduler_stuck_after_claim_commit_does_not_reclaim_live_occurrence(
    pool: _Pool, monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP
) -> None:
    row = _schedule()
    pool.rows[row.id] = row
    _cap(monkeypatch, 2)
    monkeypatch.setattr(scheduler, "compute_next_run", _next_run)
    monkeypatch.setattr(schedule_manual_run_dao, "claim_pending", AsyncMock(return_value=[]))
    execute = AsyncMock()
    monkeypatch.setattr(scheduler, "_execute_periodic_schedule", execute)
    entered, release = asyncio.Event(), asyncio.Event()

    async def audit(*_args: object, **_kwargs: object) -> None:
        entered.set()
        await release.wait()

    monkeypatch.setattr("app.services.audit_logger.write_audit_log", audit)
    monkeypatch.setattr(scheduler.asyncio, "sleep", worker_http.pacing.wait)
    worker_http.runtime.tasks.start(
        "scheduler", scheduler.start_scheduler(), requirement="loop", progress_budget_seconds=300
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        claimed = pool.rows[row.id]
        assert claimed.lease_token is not None
        assert claimed.pending_due_at == row.next_run_at
        worker_http.pacing.now += 301
        await worker_http.ready(expected=False)
        assert pool.rows[row.id] == claimed
    finally:
        release.set()
    await worker_http.pacing.paused()
    await worker_http.ready(expected=True)
    assert pool.rows[row.id] == claimed
    execute.assert_awaited_once_with(claimed)
