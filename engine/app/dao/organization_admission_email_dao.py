"""Atomic credential-email intent plus independently committed, leased delivery."""

from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import ClassVar, final
from uuid import UUID

from app.dao.base import BaseDAO
from app.db.connection import DbConnection
from app.db.pool import get_pool
from app.db.session import transaction
from app.records.organization_admission_email import CredentialEmailClaim, CredentialEmailState, NewCredentialEmail

_STATE = "request_id, status, last_error, (status = 'failed' AND payload IS NOT NULL) AS retryable"


@asynccontextmanager
async def _delivery_transaction() -> AsyncGenerator[DbConnection]:
    # Explicit db means transaction() owns this checkout, even inside a request.
    async with get_pool().connection() as raw, transaction(DbConnection(raw)) as db:
        yield db


@final
class OrganizationAdmissionEmailDAO(BaseDAO[CredentialEmailState]):
    table: ClassVar[str] = "organization_admission_credential_emails"
    pk: ClassVar[str] = "request_id"
    columns: ClassVar[tuple[str, ...]] = ("request_id", "status", "last_error")
    record_factory = staticmethod(CredentialEmailState.from_row)
    _FENCE: ClassVar[str] = """
        request_id = %(id)s AND claim_token = %(claim_token)s AND status = 'pending'
        AND lease_expires_at > %(now)s
    """

    async def enqueue(self, item: NewCredentialEmail) -> None:
        """Join the approval transaction; a second intent for one request aborts it."""
        async with self.session() as db:
            await db.execute(
                """INSERT INTO organization_admission_credential_emails
                   (request_id, identity_id, to_email, credential_fingerprint, payload, message_id)
                   VALUES (%(request_id)s, %(identity_id)s, %(to_email)s, %(fingerprint)s, %(payload)s,
                           %(message_id)s)""",
                {
                    "request_id": item.request_id,
                    "identity_id": item.identity_id,
                    "to_email": item.to_email,
                    "fingerprint": item.credential_fingerprint,
                    "payload": item.payload,
                    "message_id": f"<{item.request_id}.org-admission@maraclaw.local>",
                },
            )

    async def states(self, request_ids: Sequence[UUID]) -> dict[UUID, CredentialEmailState]:
        if not request_ids:
            return {}
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {_STATE} FROM {self.table} WHERE request_id = ANY(%(ids)s)", {"ids": list(request_ids)}
            )
        found = [CredentialEmailState.from_row(row) for row in rows]
        return {state.request_id: state for state in found}

    async def requeue(self, request_id: UUID, *, now: datetime) -> CredentialEmailState | None:
        """Operator retry: same encrypted intent back to pending; ``None`` when nothing is retryable."""
        async with self.session() as db:
            row = await db.fetchone(
                f"""UPDATE {self.table} SET status = 'pending', attempts = 0, available_at = %(now)s,
                    deliver_by = %(now)s + INTERVAL '72 hours', settled_at = NULL, last_error = NULL
                    WHERE request_id = %(id)s AND status = 'failed' AND payload IS NOT NULL
                    RETURNING {_STATE}""",
                {"id": request_id, "now": now},
            )
        return self.record_factory(row) if row is not None else None

    async def claim_batch(
        self, *, limit: int, lease_seconds: int, max_attempts: int, now: datetime
    ) -> list[CredentialEmailClaim]:
        params = {
            "limit": limit,
            "max_attempts": max_attempts,
            "now": now,
            "lease_until": now + timedelta(seconds=lease_seconds),
        }
        async with _delivery_transaction() as db:
            # Exhausted or overdue work settles as failed but keeps its payload for an operator retry.
            await db.execute(
                f"""WITH exhausted AS (
                        SELECT request_id FROM {self.table}
                        WHERE status = 'pending' AND (attempts >= %(max_attempts)s OR deliver_by <= %(now)s)
                          AND (lease_expires_at IS NULL OR lease_expires_at <= %(now)s)
                        ORDER BY available_at, request_id LIMIT %(limit)s FOR UPDATE SKIP LOCKED
                    ) UPDATE {self.table} AS email
                      SET status = 'failed', settled_at = %(now)s, claim_token = NULL, lease_expires_at = NULL,
                          last_error = CASE WHEN email.attempts >= %(max_attempts)s
                                            THEN 'attempt_limit' ELSE 'delivery_deadline' END
                      FROM exhausted WHERE email.request_id = exhausted.request_id""",
                params,
            )
            rows = await db.fetchall(
                f"""WITH due AS (
                        SELECT request_id FROM {self.table}
                        WHERE status = 'pending' AND attempts < %(max_attempts)s AND deliver_by > %(now)s
                          AND available_at <= %(now)s AND (lease_expires_at IS NULL OR lease_expires_at <= %(now)s)
                        ORDER BY available_at, request_id LIMIT %(limit)s FOR UPDATE SKIP LOCKED
                    ) UPDATE {self.table} AS email
                      SET claim_token = gen_random_uuid(), lease_expires_at = %(lease_until)s,
                          attempts = email.attempts + 1
                      FROM due, organization_admission_requests AS request
                      WHERE email.request_id = due.request_id AND request.id = email.request_id
                      RETURNING email.request_id, email.identity_id, email.to_email, email.credential_fingerprint,
                                email.payload, email.message_id, request.organization_name, email.attempts,
                                email.deliver_by, email.claim_token""",
                params,
            )
        return [CredentialEmailClaim.from_row(row) for row in rows]

    async def mark_sent(self, claim: CredentialEmailClaim, *, now: datetime) -> bool:
        """SMTP accepted: settle and delete the encrypted payload in the same statement."""
        return await self._settle(
            claim,
            "status = 'sent', payload = NULL, last_error = NULL, settled_at = %(now)s",
            {"now": now},
        )

    async def fail(self, claim: CredentialEmailClaim, *, error: str, now: datetime, drop_payload: bool = False) -> bool:
        payload = ", payload = NULL" if drop_payload else ""
        return await self._settle(
            claim,
            f"status = 'failed', last_error = %(error)s, settled_at = %(now)s{payload}",
            {"error": error, "now": now},
        )

    async def retry(self, claim: CredentialEmailClaim, *, error: str, available_at: datetime, now: datetime) -> bool:
        return await self._settle(
            claim,
            "available_at = %(available_at)s, last_error = %(error)s",
            {"error": error, "available_at": available_at, "now": now},
        )

    async def _settle(self, claim: CredentialEmailClaim, assignments: str, params: Mapping[str, object]) -> bool:
        async with _delivery_transaction() as db:
            row = await db.fetchone(
                f"""UPDATE {self.table} SET {assignments}, claim_token = NULL, lease_expires_at = NULL
                    WHERE {self._FENCE} RETURNING request_id""",
                {"id": claim.request_id, "claim_token": claim.claim_token, **params},
            )
        return row is not None


organization_admission_email_dao = OrganizationAdmissionEmailDAO()
