import asyncio
import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

import pytest
from anyio import CancelScope, fail_after

from app.records.broadcast_email import BroadcastEmailRecord
from app.services import broadcast_email_worker as worker, system_email_service as email

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

    async def attempt(_to: str, _subject: str, _body: str, *, message_id: str) -> email.EmailAttemptOutcome:
        assert message_id == row.message_id
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

    async def resolve() -> email.SystemEmailConfig:
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

    async def attempt(to: str, subject: str, body: str, *, message_id: str) -> email.EmailAttemptOutcome:
        if interruption == "sibling_error" and message_id == "<broken@example.com>":
            await entered.wait()
            raise RuntimeError("unexpected adapter error")
        return await email.attempt_email(to, subject, body, message_id=message_id)

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
