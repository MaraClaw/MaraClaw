"""Transactional acceptance and independent leased authentication mail delivery."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Final

from psycopg.pq import TransactionStatus

from app.db.connection import DbConnection
from app.db.pool import get_pool
from app.db.session import connection_ctx, transaction
from app.records.auth_email import AuthEmailClaim, NewAuthEmail

MAX_ATTEMPTS: Final = 5
LEASE_SECONDS: Final = 180
BATCH_SIZE: Final = 5
RETRY_SECONDS: Final = (60, 240, 960, 3600, 3600)


@asynccontextmanager
async def _delivery_transaction() -> AsyncGenerator[DbConnection]:
    async with get_pool().connection() as raw, transaction(DbConnection(raw)) as db:
        yield db


class AuthEmailDAO:
    async def enqueue(self, item: NewAuthEmail) -> None:
        """Join the producer transaction, isolating a caught insert failure in a savepoint."""
        async with connection_ctx() as db:
            # A fresh logical connection_ctx has not begun SQL yet; otherwise raw.transaction would commit early.
            if db.raw.info.transaction_status == TransactionStatus.IDLE:
                await db.execute("BEGIN")
            async with db.raw.transaction():
                await db.execute(
                    """INSERT INTO auth_email_jobs (id, identity_id, kind, expires_at, payload)
                   VALUES (%(id)s, %(identity_id)s, %(kind)s, %(expires_at)s, %(payload)s)""",
                    {
                        "id": item.id,
                        "identity_id": item.identity_id,
                        "kind": item.kind,
                        "expires_at": item.expires_at,
                        "payload": item.payload,
                    },
                )

    async def claim_batch(self) -> list[AuthEmailClaim]:
        """Commit bounded SKIP LOCKED claims, scrubbing exhausted or expired payloads."""
        now = datetime.now(UTC)
        params = {"now": now, "limit": BATCH_SIZE, "max": MAX_ATTEMPTS, "lease": LEASE_SECONDS}
        async with _delivery_transaction() as db:
            await db.execute(
                """WITH exhausted AS (
                       SELECT id FROM auth_email_jobs WHERE status='pending'
                       AND (expires_at <= %(now)s OR attempts >= %(max)s)
                       AND (lease_expires_at IS NULL OR lease_expires_at <= %(now)s)
                       ORDER BY available_at,id LIMIT %(limit)s FOR UPDATE SKIP LOCKED
                   ) UPDATE auth_email_jobs AS job SET status='failed', payload=NULL,
                     last_error='delivery_limit', settled_at=%(now)s, claim_token=NULL, lease_expires_at=NULL
                     FROM exhausted WHERE job.id=exhausted.id""",
                params,
            )
            rows = await db.fetchall(
                """WITH due AS (
                       SELECT id FROM auth_email_jobs WHERE status='pending' AND expires_at > %(now)s
                       AND attempts < %(max)s AND available_at <= %(now)s
                       AND (lease_expires_at IS NULL OR lease_expires_at <= %(now)s)
                       ORDER BY available_at,id LIMIT %(limit)s FOR UPDATE SKIP LOCKED
                   ) UPDATE auth_email_jobs AS job SET claim_token=gen_random_uuid(),
                     lease_expires_at=%(now)s + %(lease)s * INTERVAL '1 second', attempts=job.attempts+1
                     FROM due WHERE job.id=due.id RETURNING job.id,job.identity_id,job.kind,job.expires_at,
                     job.payload,job.attempts,job.claim_token,job.lease_expires_at""",
                params,
            )
        return [AuthEmailClaim.from_row(row) for row in rows]

    async def settle(self, claim: AuthEmailClaim, error: str | None, *, retry: bool = False) -> bool:
        """Fence every outcome; terminal outcomes always erase the bearer material."""
        now = datetime.now(UTC)
        retryable = retry and claim.attempts < MAX_ATTEMPTS and now < claim.expires_at
        available = min(now + timedelta(seconds=RETRY_SECONDS[claim.attempts - 1]), claim.expires_at)
        async with _delivery_transaction() as db:
            row = await db.fetchone(
                """UPDATE auth_email_jobs SET status=%(status)s, last_error=%(error)s,
                   payload=CASE WHEN %(retry)s THEN payload ELSE NULL END,
                   available_at=%(available)s, settled_at=CASE WHEN %(retry)s THEN NULL ELSE %(now)s END,
                   claim_token=NULL,lease_expires_at=NULL
                   WHERE id=%(id)s AND status='pending' AND claim_token=%(claim)s
                     AND lease_expires_at > %(now)s RETURNING id""",
                {
                    "id": claim.id,
                    "claim": claim.claim_token,
                    "now": now,
                    "status": "pending" if retryable else ("sent" if error is None else "failed"),
                    "error": error,
                    "retry": retryable,
                    "available": available,
                },
            )
        return row is not None


auth_email_dao = AuthEmailDAO()
