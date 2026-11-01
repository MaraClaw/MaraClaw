"""DAO for gateway_messages"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import ClassVar, final
from uuid import UUID

from app.core.json_types import uuid_from_row
from app.dao.base import BaseDAO
from app.records.gateway_message import GatewayMessageRecord
from app.records.openclaw_dispatch import BURST_ROUTING_REASON, SharedClaim
from app.records.personal_burst import BURST_CANCELLED_RESULT

_COLUMNS = (
    "id",
    "agent_id",
    "sender_agent_id",
    "sender_user_id",
    "conversation_id",
    "content",
    "status",
    "result",
    "selected_slot",
    "guest_model_ref",
    "complexity",
    "routing_reason",
    "created_at",
    "delivered_at",
    "completed_at",
)


@final
class GatewayMessageDAO(BaseDAO[GatewayMessageRecord]):
    """DAO for OpenClaw gateway message queue rows."""

    table: ClassVar[str] = "gateway_messages"
    columns: ClassVar[tuple[str, ...]] = _COLUMNS
    record_factory = staticmethod(GatewayMessageRecord.from_row)

    async def list_pending(self, agent_id: UUID) -> Sequence[GatewayMessageRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM gateway_messages "
                "WHERE agent_id = %(agent_id)s AND status = 'pending' "
                "ORDER BY created_at ASC",
                {"agent_id": agent_id},
            )
            return [GatewayMessageRecord.from_row(row) for row in rows]

    async def lock_shared_lane(self, agent_id: UUID, session_id: UUID) -> bool:
        """Take (or re-take) the shared chat-session lock that serializes claims, appends and settlements."""
        async with self.session() as db:
            parent = await db.fetchone(
                "SELECT id FROM chat_sessions WHERE id = %(session)s AND agent_id = %(agent)s "
                "AND source_channel = 'shared' FOR UPDATE",
                {"session": session_id, "agent": agent_id},
            )
            return parent is not None

    async def claim_shared(self, agent_id: UUID, session_id: UUID, *, now: datetime) -> GatewayMessageRecord | None:
        """Claim the actual oldest pending shared row, but only when it is a legacy/other-entry row.

        A Personal burst anchor (marker or batch header) is never claimable here: its readiness, fresh scope and
        routing live in ``openclaw_shared_dispatch``. The readiness gate applies to the head AFTER ``LIMIT 1``, so an
        unready head blocks every younger row instead of being skipped.
        """
        if not await self.lock_shared_lane(agent_id, session_id):
            return None
        async with self.session() as db:
            row = await db.fetchone(
                "UPDATE gateway_messages SET status = 'delivered', delivered_at = %(now)s WHERE id = ("
                "SELECT id FROM gateway_messages WHERE agent_id = %(agent)s AND conversation_id = %(conversation)s "
                "AND status = 'pending' AND NOT EXISTS (SELECT 1 FROM gateway_messages "
                "WHERE agent_id = %(agent)s AND conversation_id = %(conversation)s AND status = 'delivered') "
                "ORDER BY created_at, id LIMIT 1) "
                "AND routing_reason IS DISTINCT FROM %(burst)s "
                "AND NOT EXISTS (SELECT 1 FROM personal_burst_batches b WHERE b.gateway_id = gateway_messages.id) "
                f"RETURNING {self._select_list()}",
                {"agent": agent_id, "conversation": str(session_id), "now": now, "burst": BURST_ROUTING_REASON},
            )
            return self.record_factory(row) if row is not None else None

    async def claim_head(self, claim: SharedClaim) -> GatewayMessageRecord | None:
        """Write the final body/routing and deliver ``claim.message_id`` iff it is still the actual lane head.

        Needs the shared-session lock (the dispatch service holds it). No delivered row may exist in the lane and no
        older pending row may precede it; otherwise nothing changes.
        """
        async with self.session() as db:
            row = await db.fetchone(
                "UPDATE gateway_messages g SET status = 'delivered', delivered_at = %(now)s, content = %(content)s, "
                "selected_slot = %(slot)s, guest_model_ref = %(ref)s, complexity = %(complexity)s, "
                "routing_reason = %(reason)s "
                "WHERE g.id = %(id)s AND g.agent_id = %(agent)s AND g.conversation_id = %(conversation)s "
                "AND g.status = 'pending' AND NOT EXISTS (SELECT 1 FROM gateway_messages d "
                "WHERE d.agent_id = g.agent_id AND d.conversation_id = g.conversation_id AND d.status = 'delivered') "
                "AND NOT EXISTS (SELECT 1 FROM gateway_messages o WHERE o.agent_id = g.agent_id "
                "AND o.conversation_id = g.conversation_id AND o.status = 'pending' "
                "AND (o.created_at, o.id) < (g.created_at, g.id)) "
                f"RETURNING {self._select_list()}",
                {
                    "id": claim.message_id,
                    "agent": claim.agent_id,
                    "conversation": claim.conversation_id,
                    "content": claim.content,
                    "slot": claim.selected_slot,
                    "ref": claim.guest_model_ref,
                    "complexity": claim.complexity,
                    "reason": claim.routing_reason,
                    "now": claim.now,
                },
            )
            return self.record_factory(row) if row is not None else None

    async def cancel_anchor(self, message_id: UUID, agent_id: UUID) -> bool:
        """Fail-closed end of a pending anchor whose header is missing or already terminal (shared lock held).

        Same effect as a batch cancellation on the queue row: completed, conversation-less, fixed result, and only
        undelivered nonterminal report intents removed.
        """
        async with self.session() as db:
            row = await db.fetchone(
                "UPDATE gateway_messages SET status = 'completed', result = %(result)s, "
                "completed_at = clock_timestamp(), conversation_id = NULL "
                "WHERE id = %(id)s AND agent_id = %(agent)s AND status = 'pending' RETURNING id",
                {"id": message_id, "agent": agent_id, "result": BURST_CANCELLED_RESULT},
            )
            if row is None:
                return False
            await db.execute(
                "DELETE FROM gateway_report_deliveries WHERE message_id = %(id)s "
                "AND delivered_at IS NULL AND terminal_at IS NULL",
                {"id": message_id},
            )
            return True

    async def claim_pending(
        self, agent_id: UUID, *, now: datetime, exclude_conversation: str | None = None
    ) -> list[GatewayMessageRecord]:
        """Move every pending row to delivered and return only the rows this call moved."""
        exclusion = "AND conversation_id IS DISTINCT FROM %(exclude)s " if exclude_conversation is not None else ""
        params = {"agent_id": agent_id, "now": now}
        if exclude_conversation is not None:
            params["exclude"] = exclude_conversation
        async with self.session() as db:
            rows = await db.fetchall(
                "WITH claimed AS ("
                "UPDATE gateway_messages SET status = 'delivered', delivered_at = %(now)s "
                "WHERE agent_id = %(agent_id)s AND status = 'pending' "
                + exclusion
                + f"RETURNING {self._select_list()}"
                + f") SELECT {self._select_list()} FROM claimed ORDER BY created_at ASC",
                params,
            )
            return [GatewayMessageRecord.from_row(row) for row in rows]

    async def mark_delivered(self, message_id: UUID, agent_id: UUID) -> GatewayMessageRecord | None:
        """Claim one pending row for a wake; ``None`` when a poll or another wake already took it."""
        async with self.session() as db:
            row = await db.fetchone(
                "UPDATE gateway_messages SET status = 'delivered', delivered_at = %(now)s "
                "WHERE id = %(id)s AND agent_id = %(agent_id)s AND status = 'pending' "
                f"RETURNING {self._select_list()}",
                {"id": message_id, "agent_id": agent_id, "now": datetime.now(UTC)},
            )
            return GatewayMessageRecord.from_row(row) if row is not None else None

    async def release_delivery(self, claimed: GatewayMessageRecord) -> GatewayMessageRecord | None:
        """Return a claimed row to pending, unless a newer claim or a report has moved it since."""
        async with self.session() as db:
            row = await db.fetchone(
                "UPDATE gateway_messages SET status = 'pending', delivered_at = NULL "
                "WHERE id = %(id)s AND agent_id = %(agent_id)s AND status = 'delivered' "
                "AND delivered_at = %(delivered_at)s "
                f"RETURNING {self._select_list()}",
                {"id": claimed.id, "agent_id": claimed.agent_id, "delivered_at": claimed.delivered_at},
            )
            return GatewayMessageRecord.from_row(row) if row is not None else None

    async def requeue_stale_deliveries(
        self, agent_id: UUID, *, stale_before: datetime, exclude_conversation: str | None = None
    ) -> list[UUID]:
        """Return delivered rows with no report since ``stale_before`` (or no ``delivered_at``) to pending."""
        exclusion = "AND conversation_id IS DISTINCT FROM %(exclude)s " if exclude_conversation is not None else ""
        params = {"agent_id": agent_id, "stale_before": stale_before}
        if exclude_conversation is not None:
            params["exclude"] = exclude_conversation
        async with self.session() as db:
            rows = await db.fetchall(
                "UPDATE gateway_messages SET status = 'pending', delivered_at = NULL "
                "WHERE agent_id = %(agent_id)s AND status = 'delivered' "
                "AND (delivered_at IS NULL OR delivered_at < %(stale_before)s) " + exclusion + "RETURNING id",
                params,
            )
            return [uuid_from_row(row["id"]) for row in rows]

    async def complete(
        self, message_id: UUID, agent_id: UUID, *, result: str, now: datetime
    ) -> GatewayMessageRecord | None:
        """Record the first report for a row; ``None`` when it is unknown or already completed."""
        async with self.session() as db:
            row = await db.fetchone(
                "UPDATE gateway_messages SET status = 'completed', result = %(result)s, completed_at = %(now)s "
                "WHERE id = %(id)s AND agent_id = %(agent_id)s AND status IN ('pending', 'delivered') "
                f"RETURNING {self._select_list()}",
                {"id": message_id, "agent_id": agent_id, "result": result, "now": now},
            )
            return GatewayMessageRecord.from_row(row) if row is not None else None

    async def get_for_agent(self, message_id: UUID, agent_id: UUID) -> GatewayMessageRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM gateway_messages "
                "WHERE id = %(id)s AND agent_id = %(agent_id)s LIMIT 1",
                {"id": message_id, "agent_id": agent_id},
            )
            return GatewayMessageRecord.from_row(row) if row is not None else None

    async def list_recent(self, agent_id: UUID, *, limit: int = 50) -> Sequence[GatewayMessageRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM gateway_messages "
                "WHERE agent_id = %(agent_id)s "
                "ORDER BY created_at DESC NULLS LAST LIMIT %(limit)s",
                {"agent_id": agent_id, "limit": limit},
            )
            return [GatewayMessageRecord.from_row(row) for row in rows]

    async def completed_before(self, message: GatewayMessageRecord) -> list[GatewayMessageRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM gateway_messages WHERE agent_id = %(agent)s "
                "AND conversation_id = %(conversation)s AND status = 'completed' "
                "AND (created_at, id) < (%(created)s, %(id)s) ORDER BY created_at DESC, id DESC LIMIT 5",
                {
                    "agent": message.agent_id,
                    "conversation": message.conversation_id,
                    "created": message.created_at,
                    "id": message.id,
                },
            )
        return [self.record_factory(row) for row in reversed(rows)]


gateway_message_dao = GatewayMessageDAO()
