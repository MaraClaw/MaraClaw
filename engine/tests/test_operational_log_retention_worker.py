import asyncio
from datetime import UTC, datetime, timedelta
from typing import override

import pytest
from psycopg import OperationalError
from pydantic import ValidationError

from app.config import MAX_RETENTION_DAYS, MIN_TRIGGER_EXECUTION_RETENTION_DAYS, Settings, get_settings
from app.db.errors import DbError
from app.services import operational_log_retention_worker as worker
from worker_progress_fixtures import WorkerHTTP, worker_http

__all__ = ["worker_http"]

NOW = datetime(2026, 11, 12, tzinfo=UTC)
ACTIVITY_DAYS = 30
EXECUTION_DAYS = 14


class Clock:
    @classmethod
    def now(cls, tz: object = None) -> datetime:
        return NOW


class ScriptedPurge:
    """Scripted purge results: an int is a deleted count, an exception is raised; an empty script deletes nothing."""

    def __init__(self, script: list[int | Exception]) -> None:
        self.script: list[int | Exception] = script
        self.purges: list[tuple[datetime, int]] = []

    def next_outcome(self, cutoff: datetime, limit: int) -> int:
        self.purges.append((cutoff, limit))
        outcome = self.script.pop(0) if self.script else 0
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class ActivityDAO(ScriptedPurge):
    async def purge_older_than(self, *, older_than: datetime, limit: int) -> int:
        return self.next_outcome(older_than, limit)


class ExecutionDAO(ScriptedPurge):
    async def purge_finished_before(self, *, finished_before: datetime, limit: int) -> int:
        return self.next_outcome(finished_before, limit)


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP) -> list[float]:
    recorded: list[float] = []

    async def paced(seconds: float) -> None:
        recorded.append(seconds)
        await worker_http.pacing.wait(seconds)

    monkeypatch.setattr(worker, "sleep", paced)
    monkeypatch.setattr(worker, "datetime", Clock)
    monkeypatch.setattr(get_settings(), "ACTIVITY_LOG_RETENTION_DAYS", ACTIVITY_DAYS)
    monkeypatch.setattr(get_settings(), "TRIGGER_EXECUTION_RETENTION_DAYS", EXECUTION_DAYS)
    return recorded


def start(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, activity: object, executions: object
) -> asyncio.Task[object]:
    monkeypatch.setattr(worker, "agent_activity_log_dao", activity)
    monkeypatch.setattr(worker, "trigger_execution_dao", executions)
    return worker_http.start_loop(
        "operational_log_retention",
        worker.start_operational_log_retention_worker(),
        budget_seconds=worker.PROGRESS_BUDGET_SECONDS,
    )


async def test_empty_pass_beats_then_polls(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float]
) -> None:
    # Given neither table holds anything older than its window.
    activity, executions = ActivityDAO([]), ExecutionDAO([])
    task = start(monkeypatch, worker_http, activity, executions)
    await worker_http.pacing.paused()
    # When the budget lapses while the loop sleeps, then the next empty pass runs.
    worker_http.pacing.now += worker.PROGRESS_BUDGET_SECONDS + 1
    await worker_http.ready(expected=False)
    await worker_http.pacing.step()
    # Then that empty pass beat, and every pass purged one batch per table below its own cutoff before the poll sleep.
    await worker_http.ready(expected=True)
    assert activity.purges == [(NOW - timedelta(days=ACTIVITY_DAYS), worker.PURGE_LIMIT)] * 2
    assert executions.purges == [(NOW - timedelta(days=EXECUTION_DAYS), worker.PURGE_LIMIT)] * 2
    assert sleeps == [worker.POLL_SECONDS] * 2
    assert not task.done()


@pytest.mark.parametrize("failing", ["activity", "executions"])
async def test_database_error_on_either_table_backs_off_without_beating_and_still_attempts_the_other(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float], failing: str
) -> None:
    # Given one table whose purge fails twice, then recovers, and a healthy other table.
    broken: list[int | Exception] = [DbError("down"), OperationalError("down"), 0]
    activity = ActivityDAO(broken if failing == "activity" else [])
    executions = ExecutionDAO(broken if failing == "executions" else [])
    task = start(monkeypatch, worker_http, activity, executions)
    await worker_http.pacing.paused()
    # When the budget lapses on the first failed pass and the second failed pass is retried.
    worker_http.pacing.now += worker.PROGRESS_BUDGET_SECONDS + 1
    await worker_http.ready(expected=False)
    await worker_http.pacing.step()
    # Then failures backed off with capped_backoff and never beat, even though the other table's purge succeeded;
    # only the pass in which neither table raised restores readiness.
    await worker_http.ready(expected=False)
    assert not task.done()
    await worker_http.pacing.step()
    await worker_http.ready(expected=True)
    assert sleeps == [worker.DB_RETRY_BASE_SECONDS, worker.DB_RETRY_BASE_SECONDS * 2, worker.POLL_SECONDS]
    assert worker.DB_RETRY_CAP_SECONDS < worker.PROGRESS_BUDGET_SECONDS
    assert len(activity.purges) == len(executions.purges) == 3


async def test_full_batch_from_either_table_loops_without_sleeping(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float]
) -> None:
    # Given a backlog where one table is full while the other is short, then both are full, then both short.
    full = worker.PURGE_LIMIT
    activity, executions = ActivityDAO([full, full, 7]), ExecutionDAO([3, full, 0])
    _ = start(monkeypatch, worker_http, activity, executions)
    # When the loop first sleeps.
    await worker_http.pacing.paused()
    # Then three passes ran back to back, and only the pass where every batch was short ended in the poll sleep.
    assert len(activity.purges) == len(executions.purges) == 3
    assert sleeps == [worker.POLL_SECONDS]
    await worker_http.ready(expected=True)


@pytest.mark.parametrize("disabled", ["ACTIVITY_LOG_RETENTION_DAYS", "TRIGGER_EXECUTION_RETENTION_DAYS"])
async def test_zero_retention_for_one_table_makes_no_call_for_it(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float], disabled: str
) -> None:
    # Given one table's retention disabled.
    monkeypatch.setattr(get_settings(), disabled, 0)
    activity, executions = ActivityDAO([]), ExecutionDAO([])
    _ = start(monkeypatch, worker_http, activity, executions)
    # When the loop completes its first pass.
    await worker_http.pacing.paused()
    # Then only the enabled table was purged, and the pass beat and slept.
    skipped, purged = (activity, executions) if disabled == "ACTIVITY_LOG_RETENTION_DAYS" else (executions, activity)
    assert skipped.purges == []
    assert len(purged.purges) == 1
    assert sleeps == [worker.POLL_SECONDS]
    await worker_http.ready(expected=True)


async def test_zero_retention_for_both_tables_never_purges_and_stays_ready(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float]
) -> None:
    # Given retention disabled for both tables.
    monkeypatch.setattr(get_settings(), "ACTIVITY_LOG_RETENTION_DAYS", 0)
    monkeypatch.setattr(get_settings(), "TRIGGER_EXECUTION_RETENTION_DAYS", 0)
    activity, executions = ActivityDAO([]), ExecutionDAO([])
    task = start(monkeypatch, worker_http, activity, executions)
    await worker_http.pacing.paused()
    # When more than the progress budget passes in poll steps.
    for _ in range(4):
        await worker_http.pacing.step()
        await worker_http.ready(expected=True)
    # Then the loop beat on every pass without a single DAO call.
    assert worker_http.pacing.now > worker.PROGRESS_BUDGET_SECONDS
    assert activity.purges == []
    assert executions.purges == []
    assert sleeps == [worker.POLL_SECONDS] * 5
    assert not task.done()


async def test_hung_purge_goes_stale_without_being_cancelled(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float]
) -> None:
    # Given a purge that never returns.
    entered = asyncio.Event()

    class HungDAO(ActivityDAO):
        @override
        async def purge_older_than(self, *, older_than: datetime, limit: int) -> int:
            entered.set()
            await asyncio.Event().wait()
            return 0

    task = start(monkeypatch, worker_http, HungDAO([]), ExecutionDAO([]))
    await asyncio.wait_for(entered.wait(), 2)
    await worker_http.ready(expected=True)
    # When the progress budget lapses on the injected clock.
    worker_http.pacing.now += worker.PROGRESS_BUDGET_SECONDS + 1
    # Then readiness fails while health stays up, and the required loop keeps running.
    await worker_http.ready(expected=False)
    assert not task.done()
    assert sleeps == []


def test_settings_default_to_keeping_everything() -> None:
    # Given no configuration.
    defaults = Settings.model_validate({})
    # Then neither table is purged.
    assert defaults.ACTIVITY_LOG_RETENTION_DAYS == 0
    assert defaults.TRIGGER_EXECUTION_RETENTION_DAYS == 0


@pytest.mark.parametrize("days", [0, MIN_TRIGGER_EXECUTION_RETENTION_DAYS, 30, MAX_RETENTION_DAYS])
def test_trigger_execution_retention_accepts_zero_and_at_least_seven_days(days: int) -> None:
    loaded = Settings.model_validate({"TRIGGER_EXECUTION_RETENTION_DAYS": days})
    assert days == loaded.TRIGGER_EXECUTION_RETENTION_DAYS


@pytest.mark.parametrize("days", [1, 3, MIN_TRIGGER_EXECUTION_RETENTION_DAYS - 1, -1, MAX_RETENTION_DAYS + 1])
def test_trigger_execution_retention_rejects_one_to_six_negative_and_oversized_days(days: int) -> None:
    with pytest.raises(ValidationError) as raised:
        Settings.model_validate({"TRIGGER_EXECUTION_RETENTION_DAYS": days})
    assert [error["loc"] for error in raised.value.errors()] == [("TRIGGER_EXECUTION_RETENTION_DAYS",)]


def test_trigger_execution_retention_is_validated_when_settings_load_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an environment that asks for a three day window.
    monkeypatch.setenv("TRIGGER_EXECUTION_RETENTION_DAYS", "3")
    # Then loading settings fails.
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("days", [1, 6, 365, MAX_RETENTION_DAYS])
def test_activity_log_retention_accepts_any_positive_window(days: int) -> None:
    loaded = Settings.model_validate({"ACTIVITY_LOG_RETENTION_DAYS": days})
    assert days == loaded.ACTIVITY_LOG_RETENTION_DAYS


@pytest.mark.parametrize("days", [-1, MAX_RETENTION_DAYS + 1])
def test_activity_log_retention_rejects_negative_and_oversized_days(days: int) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"ACTIVITY_LOG_RETENTION_DAYS": days})
