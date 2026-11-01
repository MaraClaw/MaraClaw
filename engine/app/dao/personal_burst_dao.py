"""Intake-side Personal burst persistence: usage, FIFO tail, guarded batch create/append.

Every mutation requires the caller's owning transaction and takes the shared chat-session lock first (the same
lock claiming uses), so the tail it reads cannot change before the guarded write.
"""

from datetime import datetime
from typing import ClassVar, final
from uuid import UUID

from app.core.json_types import datetime_from_row, int_from_row, uuid_from_row
from app.dao.base import BaseDAO
from app.dao.personal_burst_input_dao import personal_burst_input_dao
from app.dao.personal_burst_sql import (
    BATCH_COLUMNS,
    columns_of,
    require_parent_lock,
    require_transaction,
    scope_matches,
)
from app.records.personal_burst import (
    PendingUsage,
    PersonalBurstBatch,
    PersonalBurstLimits,
    PersonalBurstScope,
    PersonalBurstTail,
    required_datetime,
)
from app.records.personal_burst_errors import PersonalBurstAnchorError, PersonalBurstLimitError
from app.records.personal_burst_input import NewBurstMember
from app.records.whatsapp_personal import PERSONAL_ROUTE_PREFIX

_TAIL_FROM = (
    "FROM gateway_messages g WHERE g.agent_id = %(agent)s AND g.conversation_id = %(conv)s "
    "AND g.status = 'pending' ORDER BY g.created_at DESC, g.id DESC LIMIT 1"
)
_USAGE = """
WITH batched AS (
    SELECT COALESCE(sum(member_count), 0)::bigint AS n, COALESCE(sum(member_bytes), 0)::bigint AS b
    FROM personal_burst_batches
    WHERE agent_id = %(agent)s AND shared_session_id = %(session)s AND state IN ('pending', 'sealed')
), legacy AS (
    SELECT count(*)::bigint AS n, COALESCE(sum(octet_length(g.content)), 0)::bigint AS b
    FROM gateway_messages g
    WHERE g.agent_id = %(agent)s AND g.conversation_id = %(conv)s AND g.status = 'pending'
      AND NOT EXISTS (SELECT 1 FROM personal_burst_batches h WHERE h.gateway_id = g.id)
      AND EXISTS (SELECT 1 FROM gateway_report_deliveries d JOIN chat_sessions r ON r.id = d.destination_id
                  WHERE d.message_id = g.id AND d.destination_kind = 'im'
                    AND starts_with(r.external_conv_id, %(prefix)s))
)
SELECT batched.n + legacy.n AS count, batched.b + legacy.b AS bytes FROM batched, legacy
"""
_CREATE = f"""
WITH t AS MATERIALIZED (SELECT clock_timestamp() AS now)
INSERT INTO personal_burst_batches (gateway_id, {", ".join(BATCH_COLUMNS[1:12])},
                                    first_at, last_at, ready_at, member_count, member_bytes)
SELECT g.id, {", ".join(f"%({c})s" for c in BATCH_COLUMNS[1:12])},
       t.now, t.now, t.now + make_interval(secs => LEAST(%(quiet)s::float8, %(max)s::float8)), 1, %(bytes)s
FROM gateway_messages g, t
WHERE g.id = %(gateway_id)s AND g.agent_id = %(agent_id)s AND g.conversation_id = %(conv)s
  AND g.status = 'pending' AND g.sender_user_id = %(sender_user_id)s
  AND NOT EXISTS (SELECT 1 FROM gateway_messages l WHERE l.agent_id = g.agent_id AND l.conversation_id = g.conversation_id
                  AND l.status = 'pending' AND (l.created_at, l.id) > (g.created_at, g.id))
RETURNING {columns_of("personal_burst_batches")}
"""
_APPEND = f"""
WITH t AS MATERIALIZED (SELECT clock_timestamp() AS now)
UPDATE personal_burst_batches b
SET last_at = t.now,
    ready_at = LEAST(b.first_at + make_interval(secs => %(max)s::float8), t.now + make_interval(secs => %(quiet)s::float8)),
    member_count = b.member_count + 1, member_bytes = b.member_bytes + %(bytes)s
FROM t
WHERE b.gateway_id = %(gateway_id)s AND b.state = 'pending' AND b.ready_at > t.now AND {scope_matches("b")}
  AND b.member_bytes + 2 * b.member_count + %(bytes)s <= %(batch_bytes)s
RETURNING {columns_of("b")}
"""


@final
class PersonalBurstDAO(BaseDAO[PersonalBurstBatch]):
    table: ClassVar[str] = "personal_burst_batches"
    pk: ClassVar[str] = "gateway_id"
    columns: ClassVar[tuple[str, ...]] = BATCH_COLUMNS
    record_factory = staticmethod(PersonalBurstBatch.from_row)

    async def pending_usage(self, agent_id: UUID, shared_session_id: UUID) -> PendingUsage:
        """Waiting Personal originals and raw bytes, excluding the in-flight (claimed) batch."""
        async with self.session() as db:
            row = await db.fetchone(
                _USAGE,
                {
                    "agent": agent_id,
                    "session": shared_session_id,
                    "conv": str(shared_session_id),
                    "prefix": PERSONAL_ROUTE_PREFIX,
                },
            )
        if row is None:
            return PendingUsage(0, 0)
        return PendingUsage(int_from_row(row["count"]), int_from_row(row["bytes"]))

    async def pending_tail(self, agent_id: UUID, shared_session_id: UUID) -> PersonalBurstTail | None:
        """The actual last pending shared gateway row, whatever produced it."""
        async with self.session() as db:
            row = await db.fetchone(
                "SELECT g.id, g.created_at " + _TAIL_FROM, {"agent": agent_id, "conv": str(shared_session_id)}
            )
        if row is None:
            return None
        gateway_id = uuid_from_row(row["id"])
        return PersonalBurstTail(gateway_id, required_datetime(row["created_at"]), await self.get(gateway_id))

    async def next_created_at(self, agent_id: UUID, shared_session_id: UUID) -> datetime:
        """``GREATEST(clock_timestamp(), tail.created_at + 1 microsecond)`` under the shared-session lock."""
        db = require_transaction("created_at reservation")
        await require_parent_lock(db, agent_id, shared_session_id)
        row = await db.fetchone(
            "SELECT GREATEST(clock_timestamp(), "
            "(SELECT max(g.created_at) FROM gateway_messages g "
            "WHERE g.agent_id = %(agent)s AND g.conversation_id = %(conv)s) + interval '1 microsecond') AS at",
            {"agent": agent_id, "conv": str(shared_session_id)},
        )
        created = datetime_from_row(row["at"]) if row is not None else None
        if created is None:
            raise RuntimeError("created_at reservation returned no timestamp")
        return created

    async def create_batch(
        self, gateway_id: UUID, scope: PersonalBurstScope, member: NewBurstMember, limits: PersonalBurstLimits
    ) -> PersonalBurstBatch:
        """Start a batch on a freshly inserted pending anchor that is still the actual FIFO tail."""
        db = require_transaction("batch create")
        if member.text_bytes > limits.batch_bytes:
            raise PersonalBurstLimitError(member.text_bytes, limits.batch_bytes)
        await require_parent_lock(db, scope.agent_id, scope.shared_session_id)
        row = await db.fetchone(
            _CREATE,
            {
                **scope.params(),
                "gateway_id": gateway_id,
                "conv": str(scope.shared_session_id),
                "quiet": limits.quiet_seconds,
                "max": limits.max_seconds,
                "bytes": member.text_bytes,
            },
        )
        if row is None:
            raise PersonalBurstAnchorError(gateway_id)
        batch = self.record_factory(row)
        _ = await personal_burst_input_dao.insert_member(gateway_id, 1, member)
        return batch

    async def append_member(
        self, scope: PersonalBurstScope, member: NewBurstMember, limits: PersonalBurstLimits
    ) -> PersonalBurstBatch | None:
        """Join the actual FIFO tail iff it is a pending, unsealed, equal-scope batch still before ``ready_at``.

        ``None`` means "not eligible": the caller reserves a new anchor. Capacity admission is the caller's.
        """
        db = require_transaction("member append")
        await require_parent_lock(db, scope.agent_id, scope.shared_session_id)
        tail = await self.pending_tail(scope.agent_id, scope.shared_session_id)
        if tail is None or tail.batch is None:
            return None
        row = await db.fetchone(
            _APPEND,
            {
                **scope.params(),
                "gateway_id": tail.gateway_id,
                "quiet": limits.quiet_seconds,
                "max": limits.max_seconds,
                "bytes": member.text_bytes,
                "batch_bytes": limits.batch_bytes,
            },
        )
        if row is None:
            return None
        batch = self.record_factory(row)
        _ = await personal_burst_input_dao.insert_member(batch.gateway_id, batch.member_count, member)
        return batch


personal_burst_dao = PersonalBurstDAO()
