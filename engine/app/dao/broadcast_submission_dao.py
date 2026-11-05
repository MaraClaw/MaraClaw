"""Broadcast idempotency receipts that commit with the fan-out they describe."""

from collections.abc import Callable, Mapping
from typing import ClassVar
from uuid import UUID, uuid4

from app.dao.base import BaseDAO
from app.records.broadcast_submission import BroadcastSubmissionClaim, BroadcastSubmissionRecord


class BroadcastSubmissionDAO(BaseDAO[BroadcastSubmissionRecord]):
    table: ClassVar[str] = "broadcast_submissions"
    columns: ClassVar[tuple[str, ...]] = (
        "id",
        "tenant_id",
        "sender_id",
        "idempotency_key",
        "fingerprint",
        "broadcast_id",
        "users_notified",
        "agents_notified",
        "emails_sent",
        "created_at",
    )
    record_factory: Callable[[Mapping[str, object]], BroadcastSubmissionRecord] = BroadcastSubmissionRecord.from_row

    async def get_for_key(
        self, tenant_id: UUID, sender_id: UUID, idempotency_key: str
    ) -> BroadcastSubmissionRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"""SELECT {self._select_list()} FROM broadcast_submissions
                    WHERE tenant_id = %(tenant_id)s AND sender_id = %(sender_id)s
                      AND idempotency_key = %(key)s""",
                {"tenant_id": tenant_id, "sender_id": sender_id, "key": idempotency_key},
            )
        return self.record_factory(row) if row is not None else None

    async def claim(
        self, *, tenant_id: UUID, sender_id: UUID, idempotency_key: str, fingerprint: str, broadcast_id: UUID
    ) -> BroadcastSubmissionClaim:
        """Claim the key inside the caller's transaction.

        A concurrent claim blocks on the unique index until the first transaction ends: after a commit
        the conflict reads that receipt, after a rollback the insert proceeds. Both run in READ COMMITTED,
        so the follow-up read sees the committed row.
        """
        async with self.session() as db:
            while True:
                row = await db.fetchone(
                    f"""INSERT INTO broadcast_submissions
                        (id, tenant_id, sender_id, idempotency_key, fingerprint, broadcast_id)
                        VALUES (%(id)s, %(tenant_id)s, %(sender_id)s, %(key)s, %(fingerprint)s, %(broadcast_id)s)
                        ON CONFLICT (tenant_id, sender_id, idempotency_key) DO NOTHING
                        RETURNING {self._select_list()}""",
                    {
                        "id": uuid4(),
                        "tenant_id": tenant_id,
                        "sender_id": sender_id,
                        "key": idempotency_key,
                        "fingerprint": fingerprint,
                        "broadcast_id": broadcast_id,
                    },
                )
                if row is not None:
                    return BroadcastSubmissionClaim(self.record_factory(row), created=True)
                # The conflicting receipt may be deleted (tenant or sender removal) before this read; retry then.
                existing = await self.get_for_key(tenant_id, sender_id, idempotency_key)
                if existing is not None:
                    return BroadcastSubmissionClaim(existing, created=False)

    async def record_counts(self, submission_id: UUID, *, users: int, agents: int, emails: int) -> None:
        async with self.session() as db:
            await db.execute(
                """UPDATE broadcast_submissions
                   SET users_notified = %(users)s, agents_notified = %(agents)s, emails_sent = %(emails)s
                   WHERE id = %(id)s""",
                {"id": submission_id, "users": users, "agents": agents, "emails": emails},
            )

    async def release(self, submission_id: UUID) -> None:
        """Drop an uncommitted claim when the broadcast is refused after claiming."""
        async with self.session() as db:
            await db.execute("DELETE FROM broadcast_submissions WHERE id = %(id)s", {"id": submission_id})


broadcast_submission_dao = BroadcastSubmissionDAO()
