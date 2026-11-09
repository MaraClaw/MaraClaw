import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

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
    settle.assert_awaited_once_with(claim, "expired")


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
