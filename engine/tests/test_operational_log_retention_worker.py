import asyncio
from datetime import UTC, datetime, timedelta
from typing import Final, override

import pytest
from psycopg import OperationalError
from pydantic import ValidationError

from app.config import (
    MAX_RETENTION_DAYS,
    MIN_CHANNEL_EVENT_RECEIPT_RETENTION_DAYS,
    MIN_GATEWAY_MESSAGE_RETENTION_DAYS,
    MIN_TRIGGER_EXECUTION_RETENTION_DAYS,
    Settings,
    get_settings,
)
from app.db.errors import DbError
from app.services import operational_log_retention_worker as worker
from worker_progress_fixtures import WorkerHTTP, worker_http

__all__ = ["worker_http"]

NOW = datetime(2026, 11, 12, tzinfo=UTC)
# Each retained table: its setting, a distinct window, and the module attribute holding its DAO.
WINDOWS: Final = {
    "ACTIVITY_LOG_RETENTION_DAYS": 30,
    "TRIGGER_EXECUTION_RETENTION_DAYS": 14,
    "GATEWAY_MESSAGE_RETENTION_DAYS": 90,
    "NOTIFICATION_RETENTION_DAYS": 45,
    "TASK_RETENTION_DAYS": 60,
    "CHANNEL_EVENT_RECEIPT_RETENTION_DAYS": 21,
    "GATEWAY_REPORT_DELIVERY_RETENTION_DAYS": 10,
    "WORKSPACE_REVISION_RETENTION_DAYS": 180,
    "AUDIT_LOG_RETENTION_DAYS": 120,
}
DAO_ATTRIBUTES: Final = {
    "ACTIVITY_LOG_RETENTION_DAYS": "agent_activity_log_dao",
    "TRIGGER_EXECUTION_RETENTION_DAYS": "trigger_execution_dao",
    "GATEWAY_MESSAGE_RETENTION_DAYS": "gateway_message_dao",
    "NOTIFICATION_RETENTION_DAYS": "notification_dao",
    "TASK_RETENTION_DAYS": "task_dao",
    "CHANNEL_EVENT_RECEIPT_RETENTION_DAYS": "channel_event_receipt_dao",
    "GATEWAY_REPORT_DELIVERY_RETENTION_DAYS": "gateway_report_delivery_dao",
    "WORKSPACE_REVISION_RETENTION_DAYS": "workspace_file_revision_dao",
    "AUDIT_LOG_RETENTION_DAYS": "audit_log_dao",
}
# Retention windows that accept any positive number of days, unlike the three with a floor.
UNBOUNDED_BELOW: Final = (
    "ACTIVITY_LOG_RETENTION_DAYS",
    "NOTIFICATION_RETENTION_DAYS",
    "TASK_RETENTION_DAYS",
    "GATEWAY_REPORT_DELIVERY_RETENTION_DAYS",
    "WORKSPACE_REVISION_RETENTION_DAYS",
    "AUDIT_LOG_RETENTION_DAYS",
)


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


class GatewayDAO(ScriptedPurge):
    async def reclaim_completed_before(self, *, completed_before: datetime, limit: int) -> int:
        return self.next_outcome(completed_before, limit)


class NotificationDAO(ScriptedPurge):
    async def purge_older_than(self, *, older_than: datetime, limit: int) -> int:
        return self.next_outcome(older_than, limit)


class TaskDAO(ScriptedPurge):
    async def purge_finished_before(self, *, finished_before: datetime, limit: int) -> int:
        return self.next_outcome(finished_before, limit)


class ReceiptDAO(ScriptedPurge):
    async def purge_older_than(self, *, older_than: datetime, limit: int) -> int:
        return self.next_outcome(older_than, limit)


class DeliveryDAO(ScriptedPurge):
    async def purge_finished_before(self, *, finished_before: datetime, limit: int) -> int:
        return self.next_outcome(finished_before, limit)


class RevisionDAO(ScriptedPurge):
    async def purge_superseded_before(self, *, created_before: datetime, limit: int) -> int:
        return self.next_outcome(created_before, limit)


FAKES: Final[dict[str, type[ScriptedPurge]]] = {
    "ACTIVITY_LOG_RETENTION_DAYS": ActivityDAO,
    "TRIGGER_EXECUTION_RETENTION_DAYS": ExecutionDAO,
    "GATEWAY_MESSAGE_RETENTION_DAYS": GatewayDAO,
    "NOTIFICATION_RETENTION_DAYS": NotificationDAO,
    "TASK_RETENTION_DAYS": TaskDAO,
    "CHANNEL_EVENT_RECEIPT_RETENTION_DAYS": ReceiptDAO,
    "GATEWAY_REPORT_DELIVERY_RETENTION_DAYS": DeliveryDAO,
    "WORKSPACE_REVISION_RETENTION_DAYS": RevisionDAO,
    "AUDIT_LOG_RETENTION_DAYS": ActivityDAO,
}


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP) -> list[float]:
    recorded: list[float] = []

    async def paced(seconds: float) -> None:
        recorded.append(seconds)
        await worker_http.pacing.wait(seconds)

    monkeypatch.setattr(worker, "sleep", paced)
    monkeypatch.setattr(worker, "datetime", Clock)
    for setting, days in WINDOWS.items():
        monkeypatch.setattr(get_settings(), setting, days)
    return recorded


def scripted(**scripts: list[int | Exception]) -> dict[str, ScriptedPurge]:
    """A fake DAO per table, each with the script given under its setting name (empty when not given)."""
    return {setting: fake(scripts.get(setting, [])) for setting, fake in FAKES.items()}


def start(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, daos: dict[str, ScriptedPurge]
) -> asyncio.Task[object]:
    for setting, dao in daos.items():
        monkeypatch.setattr(worker, DAO_ATTRIBUTES[setting], dao)
    return worker_http.start_loop(
        "operational_log_retention",
        worker.start_operational_log_retention_worker(),
        budget_seconds=worker.PROGRESS_BUDGET_SECONDS,
    )


async def test_empty_pass_beats_then_polls(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float]
) -> None:
    # Given no table holds anything older than its window.
    daos = scripted()
    task = start(monkeypatch, worker_http, daos)
    await worker_http.pacing.paused()
    # When the budget lapses while the loop sleeps, then the next empty pass runs.
    worker_http.pacing.now += worker.PROGRESS_BUDGET_SECONDS + 1
    await worker_http.ready(expected=False)
    await worker_http.pacing.step()
    # Then that empty pass beat, and every pass purged one batch per table below its own cutoff before the poll sleep.
    await worker_http.ready(expected=True)
    for setting, dao in daos.items():
        assert dao.purges == [(NOW - timedelta(days=WINDOWS[setting]), worker.PURGE_LIMIT)] * 2
    assert sleeps == [worker.POLL_SECONDS] * 2
    assert not task.done()


@pytest.mark.parametrize("failing", sorted(WINDOWS))
async def test_database_error_on_any_table_backs_off_without_beating_and_still_attempts_the_others(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float], failing: str
) -> None:
    # Given one table whose purge fails twice, then recovers, and healthy other tables.
    broken: list[int | Exception] = [DbError("down"), OperationalError("down"), 0]
    daos = scripted(**{failing: broken})
    task = start(monkeypatch, worker_http, daos)
    await worker_http.pacing.paused()
    # When the budget lapses on the first failed pass and the second failed pass is retried.
    worker_http.pacing.now += worker.PROGRESS_BUDGET_SECONDS + 1
    await worker_http.ready(expected=False)
    await worker_http.pacing.step()
    # Then failures backed off with capped_backoff and never beat, even though the other tables' purges succeeded;
    # only the pass in which no table raised restores readiness.
    await worker_http.ready(expected=False)
    assert not task.done()
    await worker_http.pacing.step()
    await worker_http.ready(expected=True)
    assert sleeps == [worker.DB_RETRY_BASE_SECONDS, worker.DB_RETRY_BASE_SECONDS * 2, worker.POLL_SECONDS]
    assert worker.DB_RETRY_CAP_SECONDS < worker.PROGRESS_BUDGET_SECONDS
    assert {len(dao.purges) for dao in daos.values()} == {3}


async def test_full_batch_from_any_table_loops_without_sleeping_and_every_table_gets_each_pass(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float]
) -> None:
    # Given a backlog where each table in turn is the only full one, then every table is short.
    full = worker.PURGE_LIMIT
    tables = list(WINDOWS)
    scripts: dict[str, list[int | Exception]] = {
        setting: [full if turn == position else 1 for turn in range(len(tables))]
        for position, setting in enumerate(tables)
    }
    daos = scripted(**scripts)
    _ = start(monkeypatch, worker_http, daos)
    # When the loop first sleeps.
    await worker_http.pacing.paused()
    # Then one more pass than tables ran back to back, each taking a batch from every table, and only the pass where
    # every batch was short ended in the poll sleep.
    assert {len(dao.purges) for dao in daos.values()} == {len(tables) + 1}
    assert sleeps == [worker.POLL_SECONDS]
    await worker_http.ready(expected=True)


@pytest.mark.parametrize("disabled", sorted(WINDOWS))
async def test_zero_retention_for_one_table_makes_no_call_for_it(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float], disabled: str
) -> None:
    # Given one table's retention disabled.
    monkeypatch.setattr(get_settings(), disabled, 0)
    daos = scripted()
    _ = start(monkeypatch, worker_http, daos)
    # When the loop completes its first pass.
    await worker_http.pacing.paused()
    # Then only the enabled tables were purged, and the pass beat and slept.
    assert {setting: len(dao.purges) for setting, dao in daos.items()} == {
        setting: int(setting != disabled) for setting in daos
    }
    assert sleeps == [worker.POLL_SECONDS]
    await worker_http.ready(expected=True)


async def test_zero_retention_for_every_table_never_purges_and_stays_ready(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float]
) -> None:
    # Given retention disabled for every table.
    for setting in WINDOWS:
        monkeypatch.setattr(get_settings(), setting, 0)
    daos = scripted()
    task = start(monkeypatch, worker_http, daos)
    await worker_http.pacing.paused()
    # When more than the progress budget passes in poll steps.
    for _ in range(4):
        await worker_http.pacing.step()
        await worker_http.ready(expected=True)
    # Then the loop beat on every pass without a single DAO call.
    assert worker_http.pacing.now > worker.PROGRESS_BUDGET_SECONDS
    assert all(dao.purges == [] for dao in daos.values())
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

    daos = scripted()
    daos["ACTIVITY_LOG_RETENTION_DAYS"] = HungDAO([])
    task = start(monkeypatch, worker_http, daos)
    await asyncio.wait_for(entered.wait(), 2)
    await worker_http.ready(expected=True)
    # When the progress budget lapses on the injected clock.
    worker_http.pacing.now += worker.PROGRESS_BUDGET_SECONDS + 1
    # Then readiness fails while health stays up, and the required loop keeps running.
    await worker_http.ready(expected=False)
    assert not task.done()
    assert sleeps == []


async def test_cancelling_mid_pass_stops_before_the_next_table(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float]
) -> None:
    # Given the second table's purge hanging after the first table's batch returned.
    entered = asyncio.Event()

    class HungExecutionDAO(ExecutionDAO):
        @override
        async def purge_finished_before(self, *, finished_before: datetime, limit: int) -> int:
            entered.set()
            await asyncio.Event().wait()
            return 0

    daos = scripted()
    daos["TRIGGER_EXECUTION_RETENTION_DAYS"] = HungExecutionDAO([])
    task = start(monkeypatch, worker_http, daos)
    await asyncio.wait_for(entered.wait(), 2)
    # When the loop is cancelled as shutdown does.
    _ = task.cancel()
    _ = await asyncio.gather(task, return_exceptions=True)
    # Then the batch that had returned stands, no later table was started, and nothing slept.
    assert len(daos["ACTIVITY_LOG_RETENTION_DAYS"].purges) == 1
    assert all(len(daos[setting].purges) == 0 for setting in list(WINDOWS)[2:])
    assert sleeps == []


def test_settings_default_to_keeping_everything() -> None:
    # Given no configuration.
    defaults = Settings.model_validate({})
    # Then no table is purged.
    for setting in WINDOWS:
        assert getattr(defaults, setting) == 0


@pytest.mark.parametrize("setting", UNBOUNDED_BELOW)
@pytest.mark.parametrize("days", [1, 6, 365, MAX_RETENTION_DAYS])
def test_retention_without_a_floor_accepts_any_positive_window(setting: str, days: int) -> None:
    loaded = Settings.model_validate({setting: days})
    assert getattr(loaded, setting) == days


@pytest.mark.parametrize("setting", UNBOUNDED_BELOW)
@pytest.mark.parametrize("days", [-1, MAX_RETENTION_DAYS + 1])
def test_retention_without_a_floor_rejects_negative_and_oversized_days(setting: str, days: int) -> None:
    with pytest.raises(ValidationError) as raised:
        Settings.model_validate({setting: days})
    assert [error["loc"] for error in raised.value.errors()] == [(setting,)]


@pytest.mark.parametrize("days", [0, MIN_CHANNEL_EVENT_RECEIPT_RETENTION_DAYS, 30, MAX_RETENTION_DAYS])
def test_channel_event_receipt_retention_accepts_zero_and_at_least_seven_days(days: int) -> None:
    loaded = Settings.model_validate({"CHANNEL_EVENT_RECEIPT_RETENTION_DAYS": days})
    assert days == loaded.CHANNEL_EVENT_RECEIPT_RETENTION_DAYS


@pytest.mark.parametrize("days", [1, 3, MIN_CHANNEL_EVENT_RECEIPT_RETENTION_DAYS - 1, -1, MAX_RETENTION_DAYS + 1])
def test_channel_event_receipt_retention_rejects_one_to_six_negative_and_oversized_days(days: int) -> None:
    with pytest.raises(ValidationError) as raised:
        Settings.model_validate({"CHANNEL_EVENT_RECEIPT_RETENTION_DAYS": days})
    assert [error["loc"] for error in raised.value.errors()] == [("CHANNEL_EVENT_RECEIPT_RETENTION_DAYS",)]


def test_channel_event_receipt_retention_is_validated_when_settings_load_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an environment that asks for a six day window.
    monkeypatch.setenv("CHANNEL_EVENT_RECEIPT_RETENTION_DAYS", "6")
    # Then loading settings fails.
    with pytest.raises(ValidationError):
        Settings()


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


@pytest.mark.parametrize("days", [0, MIN_GATEWAY_MESSAGE_RETENTION_DAYS, 30, MAX_RETENTION_DAYS])
def test_gateway_message_retention_accepts_zero_and_at_least_the_minimum(days: int) -> None:
    loaded = Settings.model_validate({"GATEWAY_MESSAGE_RETENTION_DAYS": days})
    assert days == loaded.GATEWAY_MESSAGE_RETENTION_DAYS


@pytest.mark.parametrize("days", [1, MIN_GATEWAY_MESSAGE_RETENTION_DAYS - 1, -1, MAX_RETENTION_DAYS + 1])
def test_gateway_message_retention_rejects_short_negative_and_oversized_days(days: int) -> None:
    with pytest.raises(ValidationError) as raised:
        Settings.model_validate({"GATEWAY_MESSAGE_RETENTION_DAYS": days})
    assert [error["loc"] for error in raised.value.errors()] == [("GATEWAY_MESSAGE_RETENTION_DAYS",)]


def test_gateway_message_retention_is_validated_when_settings_load_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an environment that asks for a one day window.
    monkeypatch.setenv("GATEWAY_MESSAGE_RETENTION_DAYS", "1")
    # Then loading settings fails.
    with pytest.raises(ValidationError):
        Settings()
