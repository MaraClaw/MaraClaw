import asyncio
import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from typing import Literal, override
from uuid import UUID, uuid4

import pytest
from anyio import CancelScope, fail_after
from psycopg import OperationalError

from app.db.errors import DbError
from app.records.broadcast_email import BroadcastEmailRecord
from app.runtime.tasks import OwnedTasks
from app.services import broadcast_email_worker as worker, system_email_service as email
from progress_fakes import FakeClock, Pacer

NOW = datetime(2026, 10, 23, tzinfo=UTC)


class Clock:
    @classmethod
    def now(cls, tz: object = None) -> datetime:
        return NOW


def claim() -> BroadcastEmailRecord:
    return BroadcastEmailRecord(
        id=uuid4(),
        tenant_id=uuid4(),
        broadcast_id=uuid4(),
        user_id=uuid4(),
        to_email="to@example.com",
        subject="subject",
        body="body",
        message_id="<stable@example.com>",
        status="pending",
        attempts=1,
        available_at=NOW,
        deliver_by=NOW + timedelta(days=1),
        claim_token=uuid4(),
        lease_expires_at=NOW + timedelta(seconds=180),
        last_error=None,
        created_at=NOW,
        settled_at=None,
    )


def test_cancelled_terminal_record_round_trips_for_delivery_cleanup() -> None:
    # Given: a persisted cancellation with a settlement and no worker ownership.
    cancelled = replace(claim(), status="cancelled", settled_at=NOW, claim_token=None, lease_expires_at=None)
    # When: the PostgreSQL record boundary reads the terminal variant.
    restored = BroadcastEmailRecord.from_row(asdict(cancelled))
    # Then: cancellation remains a typed terminal state rather than a pending delivery.
    assert restored == cancelled


class DeliveryDAO:
    def __init__(self) -> None:
        self.rows: list[BroadcastEmailRecord] = []
        self.settlements: list[tuple[str, BroadcastEmailRecord, str | None, datetime]] = []
        self.claims: list[tuple[int, int, datetime]] = []
        self.purges: list[tuple[datetime, int]] = []

    async def claim_batch(self, *, limit: int, lease_seconds: int, now: datetime) -> list[BroadcastEmailRecord]:
        self.claims.append((limit, lease_seconds, now))
        return self.rows[:limit]

    async def mark_sent(self, row: BroadcastEmailRecord, *, now: datetime) -> bool:
        self.settlements.append(("sent", row, None, now))
        return True

    async def retry(self, row: BroadcastEmailRecord, *, error: str, available_at: datetime, now: datetime) -> bool:
        self.settlements.append(("retry", row, error, available_at))
        return True

    async def fail(self, row: BroadcastEmailRecord, *, error: str, now: datetime) -> bool:
        self.settlements.append(("failed", row, error, now))
        return True

    async def purge_settled(self, *, older_than: datetime, limit: int) -> int:
        self.purges.append((older_than, limit))
        return 0


@pytest.fixture
def dao(monkeypatch: pytest.MonkeyPatch) -> DeliveryDAO:
    store = DeliveryDAO()
    monkeypatch.setattr(worker, "broadcast_email_dao", store)
    monkeypatch.setattr(worker, "datetime", Clock)
    return store


@pytest.mark.parametrize(
    ("kind", "attempts", "action", "delay"),
    [
        ("sent", 1, "sent", 0),
        ("permanent", 1, "failed", 0),
        ("transient", 1, "retry", 60),
        ("transient", 2, "retry", 240),
        ("transient", 3, "retry", 960),
        ("transient", 4, "retry", 3600),
        ("transient", 5, "failed", 0),
        ("not_configured", 1, "retry", 60),
        ("not_configured", 5, "failed", 0),
    ],
)
async def test_delivery_settles_explicit_outcomes_with_bounded_retries(
    monkeypatch: pytest.MonkeyPatch,
    dao: DeliveryDAO,
    kind: Literal["sent", "permanent", "transient", "not_configured"],
    attempts: int,
    action: str,
    delay: int,
) -> None:
    # Given
    row = replace(claim(), attempts=attempts)

    async def attempt(
        _to: str, _subject: str, _body: str, *, message_id: str, tenant_id: UUID
    ) -> email.EmailAttemptOutcome:
        assert message_id == row.message_id
        assert tenant_id == row.tenant_id
        return email.EmailAttemptOutcome(kind, "safe_code")

    monkeypatch.setattr(worker, "attempt_email", attempt)
    # When
    await worker._deliver(row)
    # Then
    assert dao.settlements == [(action, row, None if action == "sent" else "safe_code", NOW + timedelta(seconds=delay))]


@pytest.mark.parametrize("exhausted", ["deadline", "attempts"])
async def test_exhausted_claims_do_not_send(monkeypatch: pytest.MonkeyPatch, dao: DeliveryDAO, exhausted: str) -> None:
    # Given
    row = replace(claim(), deliver_by=NOW) if exhausted == "deadline" else replace(claim(), attempts=6)
    monkeypatch.setattr(worker, "attempt_email", lambda *_args, **_kwargs: pytest.fail("exhausted send"))
    # When
    await worker._deliver(row)
    # Then
    assert dao.settlements == [("failed", row, "delivery_limit", NOW)]


async def test_retries_never_schedule_beyond_deadline(monkeypatch: pytest.MonkeyPatch, dao: DeliveryDAO) -> None:
    # Given
    row = replace(claim(), deliver_by=NOW + timedelta(seconds=30))

    async def attempt(*_args: object, **_kwargs: object) -> email.EmailAttemptOutcome:
        return email.EmailAttemptOutcome("transient", "network")

    monkeypatch.setattr(worker, "attempt_email", attempt)
    # When
    await worker._deliver(row)
    # Then
    assert dao.settlements == [("retry", row, "network", row.deliver_by)]


async def test_poll_claim_and_retention_are_bounded(monkeypatch: pytest.MonkeyPatch, dao: DeliveryDAO) -> None:
    # Given
    elapsed = iter([0.0, 0.0, 59.0, 60.0, 60.0])
    polls: list[int] = []
    monkeypatch.setattr(worker, "monotonic", lambda: next(elapsed))

    async def poll(seconds: int) -> None:
        polls.append(seconds)
        if len(polls) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(worker, "sleep", poll)
    # When
    with pytest.raises(asyncio.CancelledError):
        await worker.start_broadcast_email_worker()
    # Then
    assert dao.claims == [(5, 180, NOW)] * 3
    assert dao.purges == [(NOW - timedelta(days=14), 500)] * 2
    assert polls == [5, 5, 5]


@pytest.mark.parametrize("boundary", ["before_send", "after_send"])
async def test_interruption_preserves_claim_without_settlement(
    monkeypatch: pytest.MonkeyPatch, dao: DeliveryDAO, boundary: str
) -> None:
    # Given
    entered = asyncio.Event()
    dao.rows = [claim()]

    async def attempt(*_args: object, **_kwargs: object) -> email.EmailAttemptOutcome:
        if boundary == "before_send":
            entered.set()
            await asyncio.Event().wait()
        return email.EmailAttemptOutcome("sent")

    async def settle(_row: BroadcastEmailRecord, *, now: datetime) -> bool:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("interrupted settlement continued")

    monkeypatch.setattr(worker, "attempt_email", attempt)
    monkeypatch.setattr(dao, "mark_sent", settle)
    task = asyncio.create_task(worker.start_broadcast_email_worker())
    # When
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        # Then
        assert dao.settlements == []
        assert len(dao.claims) == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("interruption", ["cancel", "sibling_error", "timeout"])
async def test_owned_batch_leaves_continuing_threads_without_starting_more_batches(
    monkeypatch: pytest.MonkeyPatch, dao: DeliveryDAO, interruption: str
) -> None:
    # Given
    entered, finished = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    dao.rows = [claim() for _ in range(20)]
    started = completed = 0
    lock = threading.Lock()
    native_count = 4 if interruption == "sibling_error" else 5
    deadlines: list[CancelScope] = []

    @contextmanager
    def controlled_timeout(_seconds: int) -> Generator[None]:
        with fail_after(float("inf")) as scope:
            deadlines.append(scope)
            yield

    async def resolve(_tenant_id: UUID) -> email.SystemEmailConfig:
        return email.SystemEmailConfig("from@example.com", "MaraClaw", "smtp.example.com", 465, "u", "s", True, 15)

    def smtp(**_kwargs: object) -> None:
        nonlocal started, completed
        with lock:
            started += 1
            if started == native_count:
                loop.call_soon_threadsafe(entered.set)
        try:
            release.wait(timeout=5)
        finally:
            with lock:
                completed += 1
                if completed == native_count:
                    loop.call_soon_threadsafe(finished.set)

    async def attempt(
        to: str, subject: str, body: str, *, message_id: str, tenant_id: UUID
    ) -> email.EmailAttemptOutcome:
        if interruption == "sibling_error" and message_id == "<broken@example.com>":
            await entered.wait()
            raise RuntimeError("unexpected adapter error")
        return await email.attempt_email(to, subject, body, message_id=message_id, tenant_id=tenant_id)

    if interruption == "sibling_error":
        dao.rows[0] = replace(dao.rows[0], message_id="<broken@example.com>")
    monkeypatch.setattr(email, "resolve_email_config_async", resolve)
    monkeypatch.setattr(email, "send_smtp_email", smtp)
    monkeypatch.setattr(worker, "attempt_email", attempt)
    if interruption == "timeout":
        monkeypatch.setattr(worker, "fail_after", controlled_timeout)
    task = asyncio.create_task(worker.start_broadcast_email_worker())
    # When
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        if interruption == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2)
        else:
            if interruption == "timeout":
                deadlines[0].deadline = 0
            with pytest.raises((ExceptionGroup, TimeoutError)):
                await asyncio.wait_for(task, timeout=2)
        # Then
        assert len(dao.claims) == 1
        assert dao.settlements == []
        assert started == native_count
        assert not finished.is_set()
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.wait_for(finished.wait(), timeout=2)


class FlakyDAO(DeliveryDAO):
    """Raises scripted errors from ``claim_batch`` and ``purge_settled`` before delegating."""

    def __init__(self, claim_errors: list[Exception], purge_errors: list[Exception]) -> None:
        super().__init__()
        self.claim_errors: list[Exception] = claim_errors
        self.purge_errors: list[Exception] = purge_errors

    @override
    async def claim_batch(self, *, limit: int, lease_seconds: int, now: datetime) -> list[BroadcastEmailRecord]:
        if self.claim_errors:
            raise self.claim_errors.pop(0)
        return await super().claim_batch(limit=limit, lease_seconds=lease_seconds, now=now)

    @override
    async def purge_settled(self, *, older_than: datetime, limit: int) -> int:
        if self.purge_errors:
            raise self.purge_errors.pop(0)
        return await super().purge_settled(older_than=older_than, limit=limit)


def flaky(monkeypatch: pytest.MonkeyPatch, claim_errors: list[Exception], purge_errors: list[Exception]) -> FlakyDAO:
    store = FlakyDAO(claim_errors, purge_errors)
    monkeypatch.setattr(worker, "broadcast_email_dao", store)
    monkeypatch.setattr(worker, "datetime", Clock)
    return store


async def test_claim_db_errors_retry_with_capped_backoff_below_the_progress_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given seven consecutive claim failures, then a healthy pass.
    errors: list[Exception] = [DbError("down"), OperationalError("down")] * 3 + [OSError("down")]
    store = flaky(monkeypatch, errors, [])
    sleeps: list[float] = []

    async def record(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 8:
            raise asyncio.CancelledError

    monkeypatch.setattr(worker, "sleep", record)
    # When the loop runs.
    with pytest.raises(asyncio.CancelledError):
        await worker.start_broadcast_email_worker()
    # Then it backed off 1, 2, 4, ... up to the cap, then claimed normally and resumed polling.
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, worker.POLL_SECONDS]
    assert worker.DB_RETRY_CAP_SECONDS < worker.PROGRESS_BUDGET_SECONDS
    assert len(store.claims) == 1


async def test_purge_db_error_does_not_block_claiming_and_is_retried_on_a_short_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given the first purge fails and time is a fake monotonic clock advanced only by the poll sleep.
    clock = FakeClock()
    store = flaky(monkeypatch, [], [DbError("purge down")])
    sleeps: list[float] = []

    async def poll(seconds: float) -> None:
        sleeps.append(seconds)
        clock.now += seconds
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(worker, "monotonic", clock)
    monkeypatch.setattr(worker, "sleep", poll)
    # When the loop runs three passes.
    with pytest.raises(asyncio.CancelledError):
        await worker.start_broadcast_email_worker()
    # Then every pass still claimed, and the purge succeeded on pass two (short backoff), not a 60 second interval later.
    assert len(store.claims) == 3
    assert len(store.purges) == 1


async def test_failed_claims_do_not_beat_so_readiness_goes_stale_then_recovers() -> None:
    # Given the real loop as a budgeted owned task whose claims fail.
    clock, pacer = FakeClock(), Pacer()
    tasks = OwnedTasks(clock)
    store = FlakyDAO([DbError("down")] * 2, [])
    patch = pytest.MonkeyPatch()
    patch.setattr(worker, "broadcast_email_dao", store)
    patch.setattr(worker, "datetime", Clock)
    patch.setattr(worker, "sleep", pacer.sleep)
    try:
        tasks.start(
            "broadcast_email",
            worker.start_broadcast_email_worker(),
            requirement="loop",
            progress_budget_seconds=worker.PROGRESS_BUDGET_SECONDS,
        )
        await pacer.parked()
        # When the budget lapses on failed attempts alone.
        clock.now += worker.PROGRESS_BUDGET_SECONDS + 1
        assert not tasks.ready()
        assert not tasks.required["broadcast_email"][0].done()
        await pacer.release()
        assert not tasks.ready()
        # Then a finished pass beats and readiness recovers.
        await pacer.release()
        assert tasks.ready()
    finally:
        await tasks.stop()
        patch.undo()


async def test_batch_timeout_stays_fatal_and_does_not_beat(monkeypatch: pytest.MonkeyPatch, dao: DeliveryDAO) -> None:
    # Given a stuck delivery and a batch deadline that has already expired.
    dao.rows = [claim()]
    clock = FakeClock()
    tasks = OwnedTasks(clock)
    release = asyncio.Event()

    async def attempt(*_args: object, **_kwargs: object) -> email.EmailAttemptOutcome:
        await release.wait()
        return email.EmailAttemptOutcome("sent")

    @contextmanager
    def expired(_seconds: int) -> Generator[None]:
        with fail_after(0):
            yield

    monkeypatch.setattr(worker, "attempt_email", attempt)
    monkeypatch.setattr(worker, "fail_after", expired)
    # When the required loop hits the batch timeout.
    tasks.start(
        "broadcast_email", worker.start_broadcast_email_worker(), requirement="loop", progress_budget_seconds=300
    )
    task, _ = tasks.required["broadcast_email"]
    result = (await asyncio.gather(task, return_exceptions=True))[0]
    # Then the loop ended with the timeout (no in-process restart, one claim) and readiness is not ready.
    assert isinstance(result, (ExceptionGroup, TimeoutError))
    assert len(dao.claims) == 1
    assert not tasks.ready()
    await tasks.stop()
