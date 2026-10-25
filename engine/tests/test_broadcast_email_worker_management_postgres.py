import threading
from datetime import UTC, datetime

import anyio
import pytest

from app.dao.broadcast_email_dao import broadcast_email_dao as outbox
from app.services import broadcast_email_worker as worker, system_email_service as email
from broadcast_email_persistence_fixtures import PersistenceDB, broadcast_db, enqueue

__all__ = ["broadcast_db"]


async def test_cancel_queued_sibling_while_claimed_smtp_continues(
    broadcast_db: PersistenceDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a real worker with one SMTP thread in flight and another recipient still queued.
    rows = await enqueue(broadcast_db, 2)
    scope = rows[0]
    entered, finished, polled = anyio.Event(), anyio.Event(), anyio.Event()
    release = threading.Event()
    attempts: list[str] = []

    async def resolve() -> email.SystemEmailConfig:
        return email.SystemEmailConfig("from@example.test", "MaraClaw", "smtp.example.test", 465, "u", "s", True, 15)

    def smtp(*, msg_string: str, **_kwargs: str | int | bool | list[str]) -> None:
        attempts.append(msg_string)
        anyio.from_thread.run_sync(entered.set)
        try:
            assert release.wait(timeout=10)
        finally:
            anyio.from_thread.run_sync(finished.set)

    async def poll(_seconds: int) -> None:
        polled.set()
        await anyio.sleep_forever()

    monkeypatch.setattr(email, "resolve_email_config_async", resolve)
    monkeypatch.setattr(email, "send_smtp_email", smtp)
    monkeypatch.setattr(worker, "BATCH_SIZE", 1)
    monkeypatch.setattr(worker, "sleep", poll)
    # When: cancellation commits while SMTP is blocked outside every transaction.
    try:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(worker.start_broadcast_email_worker)
            with anyio.fail_after(3):
                await entered.wait()
            result = await outbox.cancel_email(scope.tenant_id, scope.broadcast_id)
            assert result.cancelled_now == 1
            assert result.status.claimed == result.status.cancelled == 1
            release.set()
            with anyio.fail_after(3):
                await polled.wait()
            tasks.cancel_scope.cancel()
    finally:
        release.set()
        with anyio.fail_after(3):
            await finished.wait()
    # Then: only the admitted SMTP attempt completes; cancelled work never becomes claimable.
    status = await outbox.email_status(scope.tenant_id, scope.broadcast_id)
    assert (status.queued, status.claimed, status.smtp_accepted, status.cancelled) == (0, 0, 1, 1)
    assert len(attempts) == 1
    assert await outbox.claim_batch(limit=5, lease_seconds=30, now=datetime.now(UTC)) == []
