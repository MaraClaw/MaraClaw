import asyncio
from datetime import UTC, datetime, timedelta
from typing import override

import pytest
from psycopg import OperationalError

from app.config import get_settings
from app.db.errors import DbError
from app.services import chat_history_retention_worker as worker
from worker_progress_fixtures import WorkerHTTP, worker_http

__all__ = ["worker_http"]

NOW = datetime(2026, 11, 12, tzinfo=UTC)


class Clock:
    @classmethod
    def now(cls, tz: object = None) -> datetime:
        return NOW


class JournalDAO:
    """Scripted purge results: an int is a deleted count, an exception is raised; an empty script deletes nothing."""

    def __init__(self, script: list[int | Exception]) -> None:
        self.script: list[int | Exception] = script
        self.purges: list[tuple[datetime, int]] = []

    async def purge_history_changes(self, *, older_than: datetime, limit: int) -> int:
        self.purges.append((older_than, limit))
        outcome = self.script.pop(0) if self.script else 0
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP) -> list[float]:
    recorded: list[float] = []

    async def paced(seconds: float) -> None:
        recorded.append(seconds)
        await worker_http.pacing.wait(seconds)

    monkeypatch.setattr(worker, "sleep", paced)
    monkeypatch.setattr(worker, "datetime", Clock)
    monkeypatch.setattr(get_settings(), "CHAT_HISTORY_RETENTION_DAYS", 30)
    return recorded


def start(monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, dao: object) -> asyncio.Task[object]:
    monkeypatch.setattr(worker, "chat_message_dao", dao)
    return worker_http.start_loop(
        "chat_history_retention",
        worker.start_chat_history_retention_worker(),
        budget_seconds=worker.PROGRESS_BUDGET_SECONDS,
    )


async def test_empty_pass_beats_then_polls(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float]
) -> None:
    # Given a journal with nothing older than the window.
    dao = JournalDAO([])
    task = start(monkeypatch, worker_http, dao)
    await worker_http.pacing.paused()
    # When the budget lapses while the loop sleeps, then the next empty pass runs.
    worker_http.pacing.now += worker.PROGRESS_BUDGET_SECONDS + 1
    await worker_http.ready(expected=False)
    await worker_http.pacing.step()
    # Then that empty pass beat, and every pass purged one batch below the 30 day cutoff before the poll sleep.
    await worker_http.ready(expected=True)
    assert dao.purges == [(NOW - timedelta(days=30), worker.PURGE_LIMIT)] * 2
    assert sleeps == [worker.POLL_SECONDS] * 2
    assert not task.done()


async def test_database_errors_back_off_without_beating(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float]
) -> None:
    # Given two failing purges, then a healthy one.
    dao = JournalDAO([DbError("down"), OperationalError("down"), 0])
    task = start(monkeypatch, worker_http, dao)
    await worker_http.pacing.paused()
    # When the budget lapses on the first failure and the second failure is retried.
    worker_http.pacing.now += worker.PROGRESS_BUDGET_SECONDS + 1
    await worker_http.ready(expected=False)
    await worker_http.pacing.step()
    # Then failures backed off with capped_backoff and never beat; only the completed pass restores readiness.
    await worker_http.ready(expected=False)
    assert not task.done()
    await worker_http.pacing.step()
    await worker_http.ready(expected=True)
    assert sleeps == [worker.DB_RETRY_BASE_SECONDS, worker.DB_RETRY_BASE_SECONDS * 2, worker.POLL_SECONDS]
    assert worker.DB_RETRY_CAP_SECONDS < worker.PROGRESS_BUDGET_SECONDS
    assert len(dao.purges) == 3


async def test_full_batches_drain_without_sleeping(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float]
) -> None:
    # Given a backlog of two full batches and a short tail.
    dao = JournalDAO([worker.PURGE_LIMIT, worker.PURGE_LIMIT, 7])
    _ = start(monkeypatch, worker_http, dao)
    # When the loop first sleeps.
    await worker_http.pacing.paused()
    # Then all three batches ran back to back and only the short batch was followed by the poll sleep.
    assert len(dao.purges) == 3
    assert sleeps == [worker.POLL_SECONDS]
    await worker_http.ready(expected=True)


async def test_zero_retention_never_purges_and_stays_ready(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float]
) -> None:
    # Given retention disabled.
    monkeypatch.setattr(get_settings(), "CHAT_HISTORY_RETENTION_DAYS", 0)
    dao = JournalDAO([])
    task = start(monkeypatch, worker_http, dao)
    await worker_http.pacing.paused()
    # When more than the progress budget passes in poll steps.
    for _ in range(4):
        await worker_http.pacing.step()
        await worker_http.ready(expected=True)
    # Then the loop beat on every pass without a single DAO call.
    assert worker_http.pacing.now > worker.PROGRESS_BUDGET_SECONDS
    assert dao.purges == []
    assert sleeps == [worker.POLL_SECONDS] * 5
    assert not task.done()


async def test_hung_purge_goes_stale_without_being_cancelled(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, sleeps: list[float]
) -> None:
    # Given a purge that never returns.
    entered = asyncio.Event()

    class HungDAO(JournalDAO):
        @override
        async def purge_history_changes(self, *, older_than: datetime, limit: int) -> int:
            entered.set()
            await asyncio.Event().wait()
            return 0

    task = start(monkeypatch, worker_http, HungDAO([]))
    await asyncio.wait_for(entered.wait(), 2)
    await worker_http.ready(expected=True)
    # When the progress budget lapses on the injected clock.
    worker_http.pacing.now += worker.PROGRESS_BUDGET_SECONDS + 1
    # Then readiness fails while health stays up, and the required loop keeps running.
    await worker_http.ready(expected=False)
    assert not task.done()
    assert sleeps == []
