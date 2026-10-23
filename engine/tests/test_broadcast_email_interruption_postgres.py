import smtplib
import threading
from datetime import datetime, timedelta, tzinfo
from email import message_from_string

import anyio
import pytest

from app.dao.broadcast_email_dao import broadcast_email_dao as outbox
from app.records.broadcast_email import BroadcastEmailRecord
from app.services import broadcast_email_worker as worker, system_email_service as email
from broadcast_email_persistence_fixtures import PersistenceDB, broadcast_db as broadcast_db, enqueue


class Clock:
    """Mutable UTC clock to model lease expiry without elapsed-time sleeps."""

    def __init__(self, now: datetime) -> None:
        self.current: datetime = now

    def now(self, _tz: tzinfo) -> datetime:
        return self.current


@pytest.fixture(autouse=True)
def configured_email(monkeypatch: pytest.MonkeyPatch) -> None:
    async def resolve() -> email.SystemEmailConfig:
        return email.SystemEmailConfig("from@example.test", "MaraClaw", "smtp.example.test", 465, "u", "s", True, 15)

    monkeypatch.setattr(email, "resolve_email_config_async", resolve)


@pytest.mark.parametrize("accepted", [False, True], ids=["before_smtp_result", "after_smtp_acceptance"])
async def test_interrupted_worker_recovers_committed_claim_after_pool_reopen(
    broadcast_db: PersistenceDB, monkeypatch: pytest.MonkeyPatch, accepted: bool
) -> None:
    # Given: actual worker, SMTP attempt adapter and independently committed DAO.
    [original] = await enqueue(broadcast_db)
    clock = Clock(original.created_at + timedelta(seconds=1))
    monkeypatch.setattr(worker, "datetime", clock)
    entered, finished, settled = anyio.Event(), anyio.Event(), anyio.Event()
    release = threading.Event()
    message_ids: list[str] = []
    first_claims: list[BroadcastEmailRecord] = []
    real_mark_sent = outbox.mark_sent

    def smtp(*, msg_string: str, **_kwargs: str | int | bool | list[str]) -> None:
        message_ids.append(str(message_from_string(msg_string)["Message-ID"]))
        if len(message_ids) == 1:
            try:
                if not accepted:
                    anyio.from_thread.run_sync(entered.set)
                    assert release.wait(timeout=10)
            finally:
                anyio.from_thread.run_sync(finished.set)

    async def mark_sent(claim: BroadcastEmailRecord, *, now: datetime) -> bool:
        if accepted and claim.attempts == 1:
            entered.set()
            await anyio.sleep_forever()
        result = await real_mark_sent(claim, now=now)
        if result:
            settled.set()
        return result

    monkeypatch.setattr(email, "send_smtp_email", smtp)
    monkeypatch.setattr(outbox, "mark_sent", mark_sent)
    # When: shutdown interrupts an in-flight result or an accepted but unsettled send.
    try:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(worker.start_broadcast_email_worker)
            with anyio.fail_after(3):
                await entered.wait()
            claim = await outbox.get(original.id)
            assert claim is not None
            first_claims.append(claim)
            tasks.cancel_scope.cancel()
        [stale] = first_claims
        await broadcast_db.restart()
        durable = await outbox.get(original.id)
        assert durable is not None
        assert durable == stale
        assert durable.status == "pending"
        assert durable.settled_at is None
        assert durable.attempts == 1
        assert durable.claim_token is not None
        assert durable.lease_expires_at is not None
        assert await outbox.claim_batch(limit=5, lease_seconds=180, now=clock.current) == []
    finally:
        release.set()
        with anyio.fail_after(3):
            await finished.wait()

    # Then: a replacement worker reclaims the durable snapshot, rejecting its predecessor.
    clock.current = durable.lease_expires_at + timedelta(seconds=1)
    real_claim_batch = outbox.claim_batch
    reclaimed: list[BroadcastEmailRecord] = []

    async def claim_batch(*, limit: int, lease_seconds: int, now: datetime) -> list[BroadcastEmailRecord]:
        claims = await real_claim_batch(limit=limit, lease_seconds=lease_seconds, now=now)
        reclaimed.extend(claims)
        if claims:
            assert not await real_mark_sent(stale, now=now)
        return claims

    monkeypatch.setattr(outbox, "claim_batch", claim_batch)
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(worker.start_broadcast_email_worker)
        with anyio.fail_after(3):
            await settled.wait()
        tasks.cancel_scope.cancel()
    [replacement] = reclaimed
    assert replacement.claim_token is not None
    assert replacement.claim_token != stale.claim_token
    assert replacement.id == original.id
    assert replacement.attempts == 2
    assert (replacement.to_email, replacement.subject, replacement.body, replacement.message_id) == (
        original.to_email,
        original.subject,
        original.body,
        original.message_id,
    )
    assert message_ids == [original.message_id, original.message_id]
    delivered = await outbox.get(original.id)
    assert delivered is not None
    assert delivered.status == "sent"
    assert delivered.attempts == 2
    assert delivered.settled_at == clock.current


async def test_transient_recipient_does_not_prevent_sibling_sent_settlement(
    broadcast_db: PersistenceDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: two persisted recipients and a per-recipient temporary SMTP rejection.
    originals = await enqueue(broadcast_db, count=2)
    clock = Clock(max(row.created_at for row in originals) + timedelta(seconds=1))
    monkeypatch.setattr(worker, "datetime", clock)
    batch_finished = anyio.Event()

    def smtp(*, msg_string: str, **_kwargs: str | int | bool | list[str]) -> None:
        if message_from_string(msg_string)["To"] == "recipient0@example.test":
            raise smtplib.SMTPDataError(451, b"retry later")

    async def poll(_seconds: int) -> None:
        batch_finished.set()
        await anyio.sleep_forever()

    monkeypatch.setattr(email, "send_smtp_email", smtp)
    monkeypatch.setattr(worker, "sleep", poll)
    # When: one complete actual worker batch reaches its next poll.
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(worker.start_broadcast_email_worker)
        with anyio.fail_after(3):
            await batch_finished.wait()
        tasks.cancel_scope.cancel()
    # Then: the failed recipient is retryable while the sibling is durably sent.
    for original in originals:
        delivery = await outbox.get(original.id)
        assert delivery is not None
        assert delivery.attempts == 1
        assert delivery.claim_token is None
        assert delivery.lease_expires_at is None
        if original.to_email == "recipient0@example.test":
            assert delivery.status == "pending"
            assert delivery.last_error == "smtp_451"
            assert delivery.available_at == clock.current + timedelta(seconds=60)
        else:
            assert delivery.status == "sent"
            assert delivery.settled_at == clock.current
