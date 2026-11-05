"""Dispatch-side Personal burst persistence: head/readiness, seal, claim/complete states, cancellation.

State changes require the caller's owning transaction and lock the shared chat session before any batch or
gateway row (the order claiming uses). Cancellation retains the header and originals; only the queue row leaves.
"""

from typing import ClassVar, final
from uuid import UUID

from app.core.json_types import uuid_from_row
from app.dao.base import BaseDAO
from app.dao.personal_burst_sql import BATCH_COLUMNS, columns_of, lock_parent, require_transaction
from app.db.connection import DbConnection
from app.records.personal_burst import (
    BURST_CANCELLED_RESULT,
    BURST_SEPARATOR,
    BurstCancelReason,
    PersonalBurstBatch,
    PersonalBurstHead,
    required_datetime,
)

_DUE = "(b.state = 'sealed' OR (b.state = 'pending' AND b.ready_at <= clock_timestamp()))"
_ACTIVE = (
    "EXISTS (SELECT 1 FROM gateway_messages d WHERE d.agent_id = g.agent_id AND d.conversation_id = g.conversation_id "
    "AND d.status = 'delivered')"
)
_HEAD = f"""
SELECT g.id, g.created_at, (b.gateway_id IS NULL OR {_DUE}) AS due, {_ACTIVE} AS active
FROM gateway_messages g LEFT JOIN personal_burst_batches b ON b.gateway_id = g.id
WHERE g.agent_id = %(agent)s AND g.conversation_id = %(conv)s AND g.status = 'pending'
ORDER BY g.created_at, g.id LIMIT 1
"""
_OPEN = f"""
SELECT {columns_of("b")} FROM personal_burst_batches b JOIN gateway_messages g ON g.id = b.gateway_id
WHERE b.agent_id = %(agent)s AND b.shared_session_id = %(session)s AND b.state IN ('pending', 'sealed')
  AND (%(config)s::uuid IS NULL OR b.config_id = %(config)s::uuid)
ORDER BY g.created_at, g.id
"""
_SEAL = f"""
WITH m AS (
    SELECT count(*) AS n, max(ordinal) AS top, COALESCE(sum(text_bytes), 0)::bigint AS size,
           string_agg(text, %(sep)s ORDER BY ordinal) AS body
    FROM personal_burst_inputs WHERE batch_id = %(id)s AND outcome = 'accepted'
)
UPDATE personal_burst_batches b SET state = 'sealed', frozen_body = m.body, sealed_at = clock_timestamp()
FROM m
WHERE b.gateway_id = %(id)s AND b.state = 'pending' AND b.ready_at <= clock_timestamp()
  AND m.n = b.member_count AND m.top = b.member_count AND m.size = b.member_bytes
RETURNING {columns_of("b")}
"""


@final
class PersonalBurstDispatchDAO(BaseDAO[PersonalBurstBatch]):
    table: ClassVar[str] = "personal_burst_batches"
    pk: ClassVar[str] = "gateway_id"
    columns: ClassVar[tuple[str, ...]] = BATCH_COLUMNS
    record_factory = staticmethod(PersonalBurstBatch.from_row)

    async def head(self, agent_id: UUID, shared_session_id: UUID) -> PersonalBurstHead | None:
        """The ACTUAL oldest pending shared row. Readiness is reported, never used to skip an unready head."""
        async with self.session() as db:
            row = await db.fetchone(_HEAD, {"agent": agent_id, "conv": str(shared_session_id)})
        if row is None:
            return None
        gateway_id = uuid_from_row(row["id"])
        return PersonalBurstHead(
            gateway_id=gateway_id,
            created_at=required_datetime(row["created_at"]),
            due=row["due"] is True,
            active=row["active"] is True,
            batch=await self.get(gateway_id),
        )

    async def list_open(
        self, agent_id: UUID, shared_session_id: UUID, *, config_id: UUID | None = None
    ) -> list[PersonalBurstBatch]:
        """Pending and sealed (not yet claimed) batches in gateway FIFO order."""
        async with self.session() as db:
            rows = await db.fetchall(_OPEN, {"agent": agent_id, "session": shared_session_id, "config": config_id})
        return [self.record_factory(row) for row in rows]

    async def seal(self, gateway_id: UUID) -> PersonalBurstBatch | None:
        """Permanently freeze a due batch (DB clock) and return it in its resulting state.

        Not yet due: returned unchanged (``pending``). Members missing for any reason: cancelled ``members_lost``.
        """
        db = require_transaction("seal")
        batch = await self._lock(db, gateway_id)
        if batch is None or batch.state != "pending":
            return batch
        due = await db.fetchval("SELECT %(ready)s::timestamptz <= clock_timestamp() AS due", {"ready": batch.ready_at})
        if due is not True:
            return batch
        row = await db.fetchone(_SEAL, {"id": gateway_id, "sep": BURST_SEPARATOR})
        if row is None:
            return await self._cancel(db, gateway_id, "members_lost")
        return self.record_factory(row)

    async def mark_claimed(self, gateway_id: UUID) -> bool:
        """``sealed`` -> ``claimed`` (permanent: delivered turns are never requeued). Call in the claim transaction."""
        return await self._advance(gateway_id, "sealed", "claimed", "claimed_at = clock_timestamp()")

    async def mark_completed(self, gateway_id: UUID) -> bool:
        return await self._advance(gateway_id, "claimed", "completed", "finished_at = clock_timestamp()")

    async def cancel_batch(self, gateway_id: UUID, reason: BurstCancelReason) -> PersonalBurstBatch | None:
        """Cancel a pending/sealed batch; ``None`` when missing, claimed or already finished."""
        return await self._lock_and_cancel(require_transaction("cancel"), gateway_id, reason)

    async def cancel_open(
        self, agent_id: UUID, shared_session_id: UUID, reason: BurstCancelReason, *, config_id: UUID | None = None
    ) -> list[UUID]:
        """Cancel every pending/sealed batch of a lane (optionally one connector). Parent lock comes first."""
        db = require_transaction("cancel")
        _ = await lock_parent(db, agent_id, shared_session_id)
        batches = await self.list_open(agent_id, shared_session_id, config_id=config_id)
        return [b.gateway_id for b in batches if await self._lock_and_cancel(db, b.gateway_id, reason) is not None]

    async def _lock_and_cancel(
        self, db: DbConnection, gateway_id: UUID, reason: BurstCancelReason
    ) -> PersonalBurstBatch | None:
        if await self._lock(db, gateway_id) is None:
            return None
        return await self._cancel(db, gateway_id, reason)

    async def _lock(self, db: DbConnection, gateway_id: UUID) -> PersonalBurstBatch | None:
        owner = await db.fetchone(
            f"SELECT agent_id, shared_session_id FROM {self.table} WHERE gateway_id = %(id)s", {"id": gateway_id}
        )
        if owner is None:
            return None
        _ = await lock_parent(db, uuid_from_row(owner["agent_id"]), uuid_from_row(owner["shared_session_id"]))
        row = await db.fetchone(
            f"SELECT {self._select_list()} FROM {self.table} WHERE gateway_id = %(id)s FOR UPDATE", {"id": gateway_id}
        )
        return self.record_factory(row) if row is not None else None

    async def _advance(self, gateway_id: UUID, source: str, target: str, stamp: str) -> bool:
        db = require_transaction("state change")
        if await self._lock(db, gateway_id) is None:
            return False
        row = await db.fetchone(
            f"UPDATE {self.table} SET state = %(target)s, {stamp} WHERE gateway_id = %(id)s AND state = %(source)s "
            "RETURNING gateway_id",
            {"id": gateway_id, "source": source, "target": target},
        )
        return row is not None

    async def _cancel(self, db: DbConnection, gateway_id: UUID, reason: BurstCancelReason) -> PersonalBurstBatch | None:
        row = await db.fetchone(
            f"UPDATE {self.table} SET state = 'cancelled', cancel_reason = %(reason)s, finished_at = clock_timestamp() "
            f"WHERE gateway_id = %(id)s AND state IN ('pending', 'sealed') RETURNING {self._select_list()}",
            {"id": gateway_id, "reason": reason},
        )
        if row is None:
            return None
        await db.execute(
            "UPDATE gateway_messages SET status = 'completed', result = %(result)s, completed_at = clock_timestamp(), "
            "conversation_id = NULL WHERE id = %(id)s AND status = 'pending'",
            {"id": gateway_id, "result": BURST_CANCELLED_RESULT},
        )
        await db.execute(
            "DELETE FROM gateway_report_deliveries WHERE message_id = %(id)s "
            "AND delivered_at IS NULL AND terminal_at IS NULL",
            {"id": gateway_id},
        )
        return self.record_factory(row)


personal_burst_dispatch_dao = PersonalBurstDispatchDAO()
