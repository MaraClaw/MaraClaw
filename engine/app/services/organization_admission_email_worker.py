"""Bounded, leased delivery of committed initial-credential emails over the platform SMTP only."""

from datetime import UTC, datetime, timedelta
from typing import Final, assert_never

from anyio import create_task_group, fail_after, sleep
from cryptography.exceptions import InvalidTag

from app.dao.identity_dao import identity_dao
from app.dao.organization_admission_email_dao import organization_admission_email_dao as emails
from app.records.organization_admission_email import CredentialEmailClaim
from app.services.organization_admission_credentials import SUBJECT, message_body, obsolete_reason, unseal
from app.services.system_email_service import attempt_email

BATCH_SIZE: Final = 5
LEASE_SECONDS: Final = 180
ATTEMPT_TIMEOUT_SECONDS: Final = 120
MAX_ATTEMPTS: Final = 5
POLL_SECONDS: Final = 5
RETRY_DELAYS_SECONDS: Final = (60, 240, 960, 3600)


async def deliver(claim: CredentialEmailClaim) -> None:
    now = datetime.now(UTC)
    if now >= claim.deliver_by or claim.attempts > MAX_ATTEMPTS:
        _ = await emails.fail(claim, error="delivery_limit", now=now)
        return
    obsolete = obsolete_reason(await identity_dao.get(claim.identity_id), claim.credential_fingerprint)
    if obsolete is not None:
        _ = await emails.fail(claim, error=obsolete, now=now, drop_payload=True)
        return
    try:
        password = unseal(claim.request_id, claim.payload)
    except InvalidTag:
        _ = await emails.fail(claim, error="payload_unreadable", now=now, drop_payload=True)
        return
    body = message_body(organization_name=claim.organization_name, email=claim.to_email, password=password)
    outcome = await attempt_email(claim.to_email, SUBJECT, body, message_id=claim.message_id)
    now = datetime.now(UTC)
    match outcome.kind:
        case "sent":
            _ = await emails.mark_sent(claim, now=now)
        case "permanent":
            _ = await emails.fail(claim, error=outcome.error or "permanent", now=now)
        case "not_configured" | "transient":
            error = outcome.error or outcome.kind
            if claim.attempts >= MAX_ATTEMPTS or now >= claim.deliver_by:
                _ = await emails.fail(claim, error=error, now=now)
            else:
                delay = timedelta(seconds=RETRY_DELAYS_SECONDS[claim.attempts - 1])
                _ = await emails.retry(claim, error=error, available_at=min(now + delay, claim.deliver_by), now=now)
        case unreachable:
            assert_never(unreachable)


async def start_organization_admission_email_worker() -> None:
    """Own each batch through settlement; interruption leaves claims lease-recoverable.

    Pending rows committed before a restart are claimed on the first scan. A timeout or unexpected error
    ends this required loop rather than stacking abandoned SMTP threads behind fresh batches.
    """
    while True:
        claims = await emails.claim_batch(
            limit=BATCH_SIZE, lease_seconds=LEASE_SECONDS, max_attempts=MAX_ATTEMPTS, now=datetime.now(UTC)
        )
        # The claim transaction has committed. No transaction surrounds SMTP.
        with fail_after(ATTEMPT_TIMEOUT_SECONDS):
            async with create_task_group() as batch:
                for claim in claims:
                    batch.start_soon(deliver, claim)
        await sleep(POLL_SECONDS)
