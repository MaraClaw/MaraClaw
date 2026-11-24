import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from anyio import fail_after, sleep_forever
from psycopg import OperationalError, ProgrammingError

from app.db.errors import DbError
from app.records.auth_email import AuthEmailClaim, AuthEmailKind, AuthEmailMaterial
from app.services import auth_email_worker as worker
from app.services.system_email_service import EmailAttemptOutcome


def _claim() -> AuthEmailClaim:
    return AuthEmailClaim(
        uuid4(),
        uuid4(),
        AuthEmailKind.RESET,
        datetime.now(UTC) + timedelta(minutes=30),
        b"encrypted",
        1,
        uuid4(),
        datetime.now(UTC) + timedelta(minutes=3),
    )


@pytest.fixture
def successful_smtp(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    smtp = AsyncMock(return_value=EmailAttemptOutcome("sent"))
    monkeypatch.setattr(worker, "attempt_email", smtp)
    monkeypatch.setattr(
        worker,
        "unseal",
        lambda _claim: AuthEmailMaterial("to@example.com", "Member", "token", "https://trusted/reset", "hash"),
    )
    monkeypatch.setattr(worker, "eligible", AsyncMock(return_value=True))
    monkeypatch.setattr(worker, "render_email_template", AsyncMock(return_value=("subject", "body")))
    return smtp


@pytest.mark.parametrize("fault", ["once", "exhausted", "timeout"])
async def test_settlement_failure_does_not_resend_or_block_other_claims(
    monkeypatch: pytest.MonkeyPatch, successful_smtp: AsyncMock, fault: str
) -> None:
    affected, sibling, next_claim = _claim(), _claim(), _claim()
    claims = AsyncMock(side_effect=([affected, sibling], [next_claim]))
    attempts: dict[AuthEmailClaim, list[tuple[str | None, bool]]] = {}
    settled: set[AuthEmailClaim] = set()

    async def settle(claim: AuthEmailClaim, error: str | None, *, retry: bool = False) -> bool:
        writes = attempts.setdefault(claim, [])
        writes.append((error, retry))
        if claim == affected:
            if fault == "timeout":
                await sleep_forever()
            if fault == "exhausted":
                raise DbError("settlement unavailable", orig=OperationalError("connection lost"))
            if len(writes) == 1:
                raise OperationalError("connection lost")
        settled.add(claim)
        return True

    async def next_poll(seconds: float) -> None:
        if seconds == worker.POLL_SECONDS and claims.await_count == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(worker.emails, "claim_batch", claims)
    monkeypatch.setattr(worker.emails, "settle", settle)
    monkeypatch.setattr(worker, "sleep", next_poll)
    if fault == "timeout":
        monkeypatch.setattr(worker, "SETTLEMENT_TIMEOUT_SECONDS", 0.02, raising=False)

    # The outer test guard must leave room for the full settlement retry schedule.
    with fail_after(10), pytest.raises(asyncio.CancelledError):
        await worker.start_auth_email_worker()

    assert settled == ({affected, sibling, next_claim} if fault == "once" else {sibling, next_claim})
    count = {"once": 2, "exhausted": 3, "timeout": 1}[fault]
    assert attempts == {affected: [(None, False)] * count, sibling: [(None, False)], next_claim: [(None, False)]}
    assert sorted(call.kwargs["message_id"] for call in successful_smtp.await_args_list) == sorted(
        f"<{claim.id}.auth-email@maraclaw.local>" for claim in (affected, sibling, next_claim)
    )


async def test_settlement_stops_after_fence_miss(monkeypatch: pytest.MonkeyPatch, successful_smtp: AsyncMock) -> None:
    claim = _claim()
    settle = AsyncMock(side_effect=[OperationalError("acknowledgement lost"), False])
    monkeypatch.setattr(worker.emails, "settle", settle)
    monkeypatch.setattr(worker, "sleep", AsyncMock())

    await worker.deliver(claim)

    assert settle.await_count == 2
    successful_smtp.assert_awaited_once()


async def test_settlement_never_retries_after_lease_expiry(
    monkeypatch: pytest.MonkeyPatch, successful_smtp: AsyncMock
) -> None:
    claim = _claim()

    async def lose_lease(_seconds: float) -> None:
        monkeypatch.setattr(worker.datetime, "now", lambda _tz: claim.lease_expires_at)

    clock = type("Clock", (), {"now": staticmethod(datetime.now)})
    monkeypatch.setattr(worker, "datetime", clock)
    monkeypatch.setattr(worker, "sleep", lose_lease)
    settle = AsyncMock(side_effect=OperationalError("connection lost"))
    monkeypatch.setattr(worker.emails, "settle", settle)

    await worker.deliver(claim)

    settle.assert_awaited_once_with(claim, None, retry=False)
    successful_smtp.assert_awaited_once()


async def test_settlement_propagates_database_programming_errors(
    monkeypatch: pytest.MonkeyPatch, successful_smtp: AsyncMock
) -> None:
    claim = _claim()
    error = DbError("invalid statement", orig=ProgrammingError("invalid statement"))
    settle = AsyncMock(side_effect=error)
    monkeypatch.setattr(worker.emails, "settle", settle)

    with pytest.raises(DbError) as caught:
        await worker.deliver(claim)

    assert caught.value is error
    settle.assert_awaited_once_with(claim, None, retry=False)
    successful_smtp.assert_awaited_once()


@pytest.mark.parametrize("boundary", ["credential", "lease"])
async def test_expired_claim_never_starts_smtp(monkeypatch, boundary):
    claim = _claim()
    past = datetime.now(UTC) - timedelta(seconds=1)
    claim = replace(claim, **({"expires_at": past} if boundary == "credential" else {"lease_expires_at": past}))
    smtp = AsyncMock()
    settle = AsyncMock(return_value=True)
    monkeypatch.setattr(worker, "attempt_email", smtp)
    monkeypatch.setattr(worker.emails, "settle", settle)
    await worker.deliver(claim)
    smtp.assert_not_awaited()
    if boundary == "credential":
        settle.assert_awaited_once_with(claim, "expired", retry=False)
    else:
        settle.assert_not_awaited()


async def test_required_loop_recovers_first_scan_and_reports_progress(monkeypatch):
    claim = _claim()
    claims = AsyncMock(return_value=[claim])
    send = AsyncMock()
    beats = []

    class Progress:
        def beat(self):
            beats.append("completed")

    async def stop(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(worker.emails, "claim_batch", claims)
    monkeypatch.setattr(worker, "deliver", send)
    monkeypatch.setattr(worker, "loop_progress", Progress)
    monkeypatch.setattr(worker, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await worker.start_auth_email_worker()
    send.assert_awaited_once_with(claim)
    assert beats == ["completed"]


async def test_timeout_drains_owned_attempt_and_never_starts_next_batch(monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    finished = []
    claim = _claim()
    claims = AsyncMock(return_value=[claim])

    async def send(*_args, **_kwargs):
        entered.set()
        await release.wait()
        finished.append("native sender joined")
        return EmailAttemptOutcome("sent")

    monkeypatch.setattr(worker.emails, "claim_batch", claims)
    monkeypatch.setattr(worker, "ATTEMPT_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(
        worker,
        "unseal",
        lambda _claim: AuthEmailMaterial("to@example.com", "Member", "token", "https://trusted/reset", "hash"),
    )
    monkeypatch.setattr(worker, "eligible", AsyncMock(return_value=True))
    monkeypatch.setattr(worker, "render_email_template", AsyncMock(return_value=("subject", "body")))
    monkeypatch.setattr(worker, "attempt_email", send)
    settle = AsyncMock()
    monkeypatch.setattr(worker.emails, "settle", settle)
    running = asyncio.create_task(worker.start_auth_email_worker())
    await entered.wait()
    # The timeout cancels the delivery task while the owned sender is deliberately blocked.
    await asyncio.sleep(0.02)
    assert not running.done()
    release.set()
    with pytest.raises(TimeoutError):
        await running
    assert finished == ["native sender joined"]
    assert claims.await_count == 1
    settle.assert_not_awaited()
