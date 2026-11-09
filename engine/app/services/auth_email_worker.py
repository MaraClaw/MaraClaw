"""Bounded leased authentication mail; every native SMTP attempt is owned through completion."""

# asyncio shielding is required because lifespan cancels native asyncio tasks and attempt_email abandons on cancellation.
import asyncio
from contextvars import Context
from datetime import UTC, datetime
from typing import Final, assert_never

from anyio import CancelScope, create_task_group, fail_after, sleep
from cryptography.exceptions import InvalidTag
from psycopg import Error as PsycopgError
from pydantic import ValidationError
from redis.exceptions import RedisError

from app.core.logging import logger
from app.dao.auth_email_dao import auth_email_dao as emails
from app.db.errors import DbError
from app.records.auth_email import AuthEmailClaim, AuthEmailKind
from app.runtime.tasks import capped_backoff, loop_progress
from app.services.auth_email_delivery import eligible, unseal
from app.services.system_email_service import attempt_email, render_email_template

POLL_SECONDS: Final = 5
ATTEMPT_TIMEOUT_SECONDS: Final = 120
PROGRESS_BUDGET_SECONDS: Final = 300.0


async def deliver(claim: AuthEmailClaim) -> None:
    if datetime.now(UTC) >= min(claim.expires_at, claim.lease_expires_at):
        _ = await emails.settle(claim, "expired")
        return
    try:
        material = unseal(claim)
    except InvalidTag, ValidationError, ValueError:
        _ = await emails.settle(claim, "payload_unreadable")
        return
    minutes = str(max(0, int((claim.expires_at - datetime.now(UTC)).total_seconds() // 60)))
    match claim.kind:
        case AuthEmailKind.VERIFICATION:
            template = "email_verification"
            variables = {"verification_code": material.credential, "verification_url": material.url}
        case AuthEmailKind.RESET:
            template = "password_reset"
            variables = {"reset_url": material.url}
        case unreachable:
            assert_never(unreachable)
    subject, body = await render_email_template(
        template, {**variables, "display_name": material.display_name, "expiry_minutes": minutes}
    )
    try:
        current = await eligible(claim, material)
    except RedisError, OSError, TimeoutError, ValueError:
        _ = await emails.settle(claim, "eligibility_unavailable", retry=True)
        return
    if not current:
        _ = await emails.settle(claim, "credential_obsolete")
        return
    if datetime.now(UTC) >= min(claim.expires_at, claim.lease_expires_at):
        _ = await emails.settle(claim, "expired")
        return
    # Never cancel the SMTP child: attempt_email's thread otherwise outlives the worker.
    sending = asyncio.create_task(
        attempt_email(material.to_email, subject, body, message_id=f"<{claim.id}.auth-email@maraclaw.local>"),
        context=Context(),
    )
    try:
        outcome = await asyncio.shield(sending)
    except asyncio.CancelledError:
        with CancelScope(shield=True):
            while not sending.done():
                try:
                    await asyncio.shield(sending)
                except asyncio.CancelledError:
                    continue
        # Retrieving an unexpected child error still ends this batch; no next batch can stack SMTP threads.
        sending.result()
        raise
    match outcome.kind:
        case "sent":
            _ = await emails.settle(claim, None)
        case "permanent":
            _ = await emails.settle(claim, outcome.error or "permanent")
        case "not_configured" | "transient":
            _ = await emails.settle(claim, outcome.error or outcome.kind, retry=True)
        case unreachable:
            assert_never(unreachable)


async def start_auth_email_worker() -> None:
    """Recover committed jobs on every scan; cancellation drains SMTP before propagating."""
    progress = loop_progress()
    failures = 0
    while True:
        try:
            claims = await emails.claim_batch()
        except (DbError, PsycopgError, OSError) as exc:
            logger.warning("Auth email claim failed; retrying: {}", type(exc).__name__)
            await sleep(capped_backoff(failures, base_seconds=1.0, cap_seconds=30.0))
            failures += 1
            continue
        failures = 0
        with fail_after(ATTEMPT_TIMEOUT_SECONDS):
            async with create_task_group() as batch:
                for claim in claims:
                    batch.start_soon(deliver, claim)
        progress.beat()
        await sleep(POLL_SECONDS)
