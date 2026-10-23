"""Bounded, leased delivery of committed broadcast email outbox rows."""

from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Final, assert_never

from anyio import create_task_group, fail_after, sleep

from app.dao.broadcast_email_dao import broadcast_email_dao
from app.records.broadcast_email import BroadcastEmailRecord
from app.services.system_email_service import attempt_email

BATCH_SIZE: Final = 5
LEASE_SECONDS: Final = 180
ATTEMPT_TIMEOUT_SECONDS: Final = 120
MAX_ATTEMPTS: Final = 5
POLL_SECONDS: Final = 5
RETENTION_DAYS: Final = 14
PURGE_LIMIT: Final = 500
PURGE_INTERVAL_SECONDS: Final = 60


async def _deliver(claim: BroadcastEmailRecord) -> None:
    now = datetime.now(UTC)
    if now >= claim.deliver_by or claim.attempts > MAX_ATTEMPTS:
        await broadcast_email_dao.fail(claim, error="delivery_limit", now=now)
        return
    outcome = await attempt_email(claim.to_email, claim.subject, claim.body, message_id=claim.message_id)
    now = datetime.now(UTC)
    match outcome.kind:
        case "sent":
            await broadcast_email_dao.mark_sent(claim, now=now)
        case "permanent":
            await broadcast_email_dao.fail(claim, error=outcome.error or "permanent", now=now)
        case "not_configured" | "transient":
            error = outcome.error or outcome.kind
            if claim.attempts >= MAX_ATTEMPTS or now >= claim.deliver_by:
                await broadcast_email_dao.fail(claim, error=error, now=now)
            else:
                delay = (60, 240, 960, 3600)[claim.attempts - 1]
                await broadcast_email_dao.retry(
                    claim,
                    error=error,
                    available_at=min(now + timedelta(seconds=delay), claim.deliver_by),
                    now=now,
                )
        case unreachable:
            assert_never(unreachable)


async def start_broadcast_email_worker() -> None:
    """Own each batch through settlement; interruption leaves claims lease-recoverable.

    A timeout/unexpected error is fatal to this required loop, not a new polling
    cycle: abandoned SMTP threads must never accumulate behind fresh batches.
    """
    next_purge = 0.0
    while True:
        now = datetime.now(UTC)
        if monotonic() >= next_purge:
            await broadcast_email_dao.purge_settled(older_than=now - timedelta(days=RETENTION_DAYS), limit=PURGE_LIMIT)
            next_purge = monotonic() + PURGE_INTERVAL_SECONDS
        claims = await broadcast_email_dao.claim_batch(limit=BATCH_SIZE, lease_seconds=LEASE_SECONDS, now=now)
        # The claim transaction has committed. No transaction surrounds SMTP.
        # This bound also covers config resolution and settlement, below the lease.
        with fail_after(ATTEMPT_TIMEOUT_SECONDS):
            async with create_task_group() as batch:
                for claim in claims:
                    batch.start_soon(_deliver, claim)
        await sleep(POLL_SECONDS)
