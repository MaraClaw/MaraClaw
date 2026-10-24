"""Transactional recipient acceptance and independently committed leased delivery."""

from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import ClassVar
from uuid import UUID, uuid4

from pydantic import TypeAdapter

from app.dao.base import BaseDAO
from app.db.connection import DbConnection
from app.db.pool import get_pool
from app.db.session import transaction
from app.records.broadcast_email import (
    BroadcastEmailCancellation,
    BroadcastEmailRecord,
    BroadcastEmailStatus,
    NewBroadcastEmail,
)


@asynccontextmanager
async def _delivery_transaction() -> AsyncGenerator[DbConnection]:
    # Explicit db means transaction() owns this checkout, even inside a request.
    async with get_pool().connection() as raw, transaction(DbConnection(raw)) as db:
        yield db


async def _email_status(db: DbConnection, tenant_id: UUID, broadcast_id: UUID) -> BroadcastEmailStatus:
    [row] = await db.fetchall(
        """SELECT %(broadcast_id)s::uuid AS broadcast_id, count(*) > 0 AS available,
                  count(*) FILTER (WHERE status = 'pending' AND claim_token IS NULL) AS queued,
                  count(*) FILTER (WHERE status = 'pending' AND claim_token IS NOT NULL) AS claimed,
                  count(*) FILTER (WHERE status = 'sent') AS smtp_accepted,
                  count(*) FILTER (WHERE status = 'failed') AS failed,
                  count(*) FILTER (WHERE status = 'cancelled') AS cancelled
           FROM broadcast_emails WHERE tenant_id = %(tenant_id)s AND broadcast_id = %(broadcast_id)s""",
        {"tenant_id": tenant_id, "broadcast_id": broadcast_id},
    )
    return BroadcastEmailStatus.from_row(row)


class BroadcastEmailDAO(BaseDAO[BroadcastEmailRecord]):
    table: ClassVar[str] = "broadcast_emails"
    columns: ClassVar[tuple[str, ...]] = (
        "id",
        "tenant_id",
        "broadcast_id",
        "user_id",
        "to_email",
        "subject",
        "body",
        "message_id",
        "status",
        "attempts",
        "available_at",
        "deliver_by",
        "claim_token",
        "lease_expires_at",
        "last_error",
        "created_at",
        "settled_at",
    )
    record_factory: Callable[[Mapping[str, object]], BroadcastEmailRecord] = BroadcastEmailRecord.from_row
    _FENCE: ClassVar[str] = """
        id = %(id)s AND claim_token = %(claim_token)s AND status = 'pending'
        AND lease_expires_at > %(now)s
    """

    async def enqueue_many(self, items: Sequence[NewBroadcastEmail]) -> int:
        """Join the acceptance transaction; duplicate recipients abort that transaction."""
        if not items:
            return 0
        rows: list[dict[str, object]] = []
        for item in items:
            delivery_id = uuid4()
            rows.append(
                {
                    "id": delivery_id,
                    "tenant_id": item.tenant_id,
                    "broadcast_id": item.broadcast_id,
                    "user_id": item.user_id,
                    "to_email": item.to_email,
                    "subject": item.subject,
                    "body": item.body,
                    "message_id": f"<{delivery_id}.broadcast@maraclaw.local>",
                }
            )
        async with self.session() as db:
            await db.executemany(
                """INSERT INTO broadcast_emails
                   (id, tenant_id, broadcast_id, user_id, to_email, subject, body, message_id)
                   VALUES (%(id)s, %(tenant_id)s, %(broadcast_id)s, %(user_id)s,
                           %(to_email)s, %(subject)s, %(body)s, %(message_id)s)""",
                rows,
            )
        return len(rows)

    async def email_status(self, tenant_id: UUID, broadcast_id: UUID) -> BroadcastEmailStatus:
        async with self.session() as db:
            return await _email_status(db, tenant_id, broadcast_id)

    async def cancel_email(self, tenant_id: UUID, broadcast_id: UUID) -> BroadcastEmailCancellation:
        async with _delivery_transaction() as db:
            count = await db.fetchval(
                """WITH cancelled AS (
                       UPDATE broadcast_emails SET status = 'cancelled', settled_at = clock_timestamp()
                       WHERE tenant_id = %(tenant_id)s AND broadcast_id = %(broadcast_id)s
                         AND status = 'pending' AND claim_token IS NULL
                       RETURNING id
                   ) SELECT count(*) FROM cancelled""",
                {"tenant_id": tenant_id, "broadcast_id": broadcast_id},
            )
            status = await _email_status(db, tenant_id, broadcast_id)
        return BroadcastEmailCancellation(status, TypeAdapter(int).validate_python(count))

    async def claim_batch(self, *, limit: int, lease_seconds: int, now: datetime) -> list[BroadcastEmailRecord]:
        if limit <= 0:
            return []
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        params = {"limit": limit, "now": now, "lease_until": now + timedelta(seconds=lease_seconds)}
        async with _delivery_transaction() as db:
            # Both housekeeping and claiming are bounded and skip other workers' locks.
            await db.execute(
                """WITH exhausted AS (
                       SELECT id FROM broadcast_emails
                       WHERE status = 'pending' AND (attempts >= 5 OR deliver_by <= %(now)s)
                         AND (lease_expires_at IS NULL OR lease_expires_at <= %(now)s)
                       ORDER BY available_at, id LIMIT %(limit)s FOR UPDATE SKIP LOCKED
                   ) UPDATE broadcast_emails AS email
                     SET status = 'failed', settled_at = %(now)s, claim_token = NULL, lease_expires_at = NULL,
                         last_error = CASE WHEN email.attempts >= 5 THEN 'attempt_limit' ELSE 'delivery_deadline' END
                     FROM exhausted WHERE email.id = exhausted.id""",
                params,
            )
            rows = await db.fetchall(
                f"""WITH due AS (
                        SELECT id FROM broadcast_emails
                        WHERE status = 'pending' AND attempts < 5 AND deliver_by > %(now)s
                          AND available_at <= %(now)s
                          AND (lease_expires_at IS NULL OR lease_expires_at <= %(now)s)
                        ORDER BY available_at, id LIMIT %(limit)s FOR UPDATE SKIP LOCKED
                    ) UPDATE broadcast_emails AS email
                      SET claim_token = gen_random_uuid(), lease_expires_at = %(lease_until)s,
                          attempts = email.attempts + 1
                      FROM due WHERE email.id = due.id RETURNING {self._select_list("email")}""",
                params,
            )
        return [self.record_factory(row) for row in rows]

    async def mark_sent(self, claim: BroadcastEmailRecord, *, now: datetime) -> bool:
        async with _delivery_transaction() as db:
            row = await db.fetchone(
                f"""UPDATE broadcast_emails SET status = 'sent', settled_at = %(now)s,
                    claim_token = NULL, lease_expires_at = NULL, last_error = NULL
                    WHERE {self._FENCE} RETURNING id""",
                {"id": claim.id, "claim_token": claim.claim_token, "now": now},
            )
        return row is not None

    async def retry(self, claim: BroadcastEmailRecord, *, error: str, available_at: datetime, now: datetime) -> bool:
        async with _delivery_transaction() as db:
            row = await db.fetchone(
                f"""UPDATE broadcast_emails
                    SET status = CASE WHEN attempts >= 5 OR deliver_by <= %(now)s OR deliver_by <= %(available_at)s
                                      THEN 'failed' ELSE 'pending' END,
                        settled_at = CASE WHEN attempts >= 5 OR deliver_by <= %(now)s OR deliver_by <= %(available_at)s
                                          THEN %(now)s ELSE NULL END,
                        available_at = %(available_at)s, last_error = %(error)s,
                        claim_token = NULL, lease_expires_at = NULL
                    WHERE {self._FENCE} RETURNING id""",
                {
                    "id": claim.id,
                    "claim_token": claim.claim_token,
                    "now": now,
                    "available_at": available_at,
                    "error": error,
                },
            )
        return row is not None

    async def fail(self, claim: BroadcastEmailRecord, *, error: str, now: datetime) -> bool:
        async with _delivery_transaction() as db:
            row = await db.fetchone(
                f"""UPDATE broadcast_emails SET status = 'failed', settled_at = %(now)s,
                    claim_token = NULL, lease_expires_at = NULL, last_error = %(error)s
                    WHERE {self._FENCE} RETURNING id""",
                {"id": claim.id, "claim_token": claim.claim_token, "now": now, "error": error},
            )
        return row is not None

    async def purge_settled(self, *, older_than: datetime, limit: int) -> int:
        if limit <= 0:
            return 0
        async with _delivery_transaction() as db:
            rows = await db.fetchall(
                """WITH expired AS (
                       SELECT id FROM broadcast_emails
                       WHERE status IN ('sent', 'failed', 'cancelled') AND settled_at < %(cutoff)s
                       ORDER BY settled_at, id LIMIT %(limit)s FOR UPDATE SKIP LOCKED
                   ) DELETE FROM broadcast_emails AS email USING expired
                     WHERE email.id = expired.id RETURNING email.id""",
                {"cutoff": older_than, "limit": limit},
            )
        return len(rows)


broadcast_email_dao = BroadcastEmailDAO()
