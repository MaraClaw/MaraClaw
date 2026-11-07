"""DAO for gateway_messages"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, final
from uuid import UUID

from app.dao.base import BaseDAO
from app.records.gateway_message import GatewayMessageRecord
from app.records.openclaw_dispatch import SharedClaim
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
    "reply_binding_id",
)


@dataclass(frozen=True, slots=True)
class PendingDuplicateProbe:
    """The identity of an incoming user message for pending-duplicate suppression."""

    agent_id: UUID
    sender_user_id: UUID
    conversation_id: str | None
    content: str


@final
class GatewayMessageDAO(BaseDAO[GatewayMessageRecord]):
    """DAO for OpenClaw gateway message queue rows."""

    table: ClassVar[str] = "gateway_messages"
    columns: ClassVar[tuple[str, ...]] = _COLUMNS
    record_factory = staticmethod(GatewayMessageRecord.from_row)

    async def lane_head(self, member: GatewayMessageRecord) -> GatewayMessageRecord | None:
        """Oldest pending row of ``member``'s lane by ``(created_at, id)``; no delivered-row or burst filter.

        A NULL or empty ``conversation_id`` is a per-message lane, so its only possible head is the row itself.
        """
        async with self.session() as db:
            if member.conversation_id not in (None, ""):
                row = await db.fetchone(
                    f"SELECT {self._select_list()} FROM gateway_messages "
                    "WHERE agent_id = %(agent)s AND conversation_id = %(conversation)s AND status = 'pending' "
                    "ORDER BY created_at, id LIMIT 1",
                    {"agent": member.agent_id, "conversation": member.conversation_id},
                )
            else:
                row = await db.fetchone(
                    f"SELECT {self._select_list()} FROM gateway_messages "
                    "WHERE agent_id = %(agent)s AND id = %(id)s AND status = 'pending'",
                    {"agent": member.agent_id, "id": member.id},
                )
            return self.record_factory(row) if row is not None else None

    async def lane_heads_page(
        self, agent_id: UUID, limit: int, *, after: tuple[datetime, UUID] | None = None
    ) -> tuple[list[GatewayMessageRecord], tuple[datetime, UUID] | None]:
        """Page one agent's distinct pending lane heads by ``(created_at, id)``.

        Same lane rule as ``lane_head``: heads are pending-only and NULL/empty conversations are per-message
        lanes. Delivered rows and burst readiness stay with the ``claim_*`` methods.
        """
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list('g')} FROM gateway_messages g "
                "WHERE g.agent_id = %(agent)s AND g.status = 'pending' AND (%(after_time)s::timestamptz IS NULL "
                "OR (g.created_at, g.id) > (%(after_time)s, %(after_id)s)) "
                "AND (g.conversation_id IS NULL OR g.conversation_id = '' OR "
                "NOT EXISTS (SELECT 1 FROM gateway_messages p WHERE p.agent_id = g.agent_id "
                "AND p.conversation_id = g.conversation_id AND p.status = 'pending' "
                "AND (p.created_at, p.id) < (g.created_at, g.id))) "
                "ORDER BY g.created_at, g.id LIMIT %(limit)s",
                {
                    "agent": agent_id,
                    "after_time": after[0] if after else None,
                    "after_id": after[1] if after else None,
                    "limit": limit + 1,
                },
            )
        messages = [self.record_factory(row) for row in rows[:limit]]
        last = messages[-1] if len(rows) > limit else None
        cursor = (last.created_at, last.id) if last is not None and last.created_at is not None else None
        return messages, cursor

    async def earliest_pending_duplicate(self, probe: PendingDuplicateProbe) -> GatewayMessageRecord | None:
        """Earliest pending row with the probe's sender and content in its lane, on the caller's connection.

        NULL and empty conversation IDs are the same (anonymous) lane here.
        """
        async with self.session() as db:
            lane = (
                "conversation_id = %(conversation)s"
                if probe.conversation_id not in (None, "")
                else "(conversation_id IS NULL OR conversation_id = '')"
            )
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM gateway_messages "
                "WHERE agent_id = %(agent)s AND status = 'pending' AND sender_user_id = %(sender)s "
                f"AND content = %(content)s AND {lane} ORDER BY created_at, id LIMIT 1",
                {
                    "agent": probe.agent_id,
                    "sender": probe.sender_user_id,
                    "content": probe.content,
                    "conversation": probe.conversation_id,
                },
            )
            return self.record_factory(row) if row is not None else None

    async def lock_shared_lane(self, agent_id: UUID, session_id: UUID) -> bool:
        """Take (or re-take) the shared chat-session lock that serializes claims, appends and settlements."""
        async with self.session() as db:
            parent = await db.fetchone(
                "SELECT id FROM chat_sessions WHERE id = %(session)s AND agent_id = %(agent)s "
                "AND source_channel = 'shared' FOR UPDATE",
                {"session": session_id, "agent": agent_id},
            )
            return parent is not None

    async def claim_head(self, claim: SharedClaim) -> GatewayMessageRecord | None:
        """Write the final body/routing and deliver ``claim.message_id`` iff it is still the actual lane head.

        Needs the shared-session lock (the dispatch service holds it). No delivered row may exist in the lane and no
        older pending row may precede it; otherwise nothing changes.
        """
        async with self.session() as db:
            row = await db.fetchone(
                "UPDATE gateway_messages g SET status = 'delivered', delivered_at = %(now)s, content = %(content)s, "
                "selected_slot = %(slot)s, guest_model_ref = %(ref)s, complexity = %(complexity)s, "
                "routing_reason = %(reason)s, "
                "reply_binding_id = bind_openclaw_reply(g.agent_id, g.conversation_id) "
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

    async def claim_ordinary_head(self, message_id: UUID, agent_id: UUID) -> GatewayMessageRecord | None:
        """Claim an ordinary lane head after the caller has locked its agent row."""
        async with self.session() as db:
            row = await db.fetchone(
                "UPDATE gateway_messages g SET status = 'delivered', delivered_at = clock_timestamp(), "
                "reply_binding_id = bind_openclaw_reply(g.agent_id, g.conversation_id) "
                "WHERE g.id = %(id)s AND g.agent_id = %(agent)s AND g.status = 'pending' "
                "AND (g.conversation_id IS NULL OR g.conversation_id = '' OR ("
                "NOT EXISTS (SELECT 1 FROM gateway_messages d WHERE d.agent_id = g.agent_id "
                "AND d.conversation_id = g.conversation_id AND d.status = 'delivered') "
                "AND NOT EXISTS (SELECT 1 FROM gateway_messages p WHERE p.agent_id = g.agent_id "
                "AND p.conversation_id = g.conversation_id AND p.status = 'pending' "
                "AND (p.created_at, p.id) < (g.created_at, g.id)))) "
                f"RETURNING {self._select_list()}",
                {"id": message_id, "agent": agent_id},
            )
            return self.record_factory(row) if row is not None else None

    async def pending_dispatch_page(
        self, limit: int, *, after: tuple[datetime, UUID] | None = None
    ) -> tuple[list[GatewayMessageRecord], tuple[datetime, UUID] | None]:
        """Page pending lane heads without replaying any delivered work."""
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list('g')} FROM gateway_messages g "
                "LEFT JOIN personal_burst_batches b ON b.gateway_id = g.id "
                "WHERE g.status = 'pending' AND (%(after_time)s::timestamptz IS NULL "
                "OR (g.created_at, g.id) > (%(after_time)s, %(after_id)s)) "
                "AND (b.gateway_id IS NULL OR b.state != 'pending' OR b.ready_at <= clock_timestamp()) "
                "AND (g.conversation_id IS NULL OR g.conversation_id = '' OR ("
                "NOT EXISTS (SELECT 1 FROM gateway_messages d WHERE d.agent_id = g.agent_id "
                "AND d.conversation_id = g.conversation_id AND d.status = 'delivered') "
                "AND NOT EXISTS (SELECT 1 FROM gateway_messages p WHERE p.agent_id = g.agent_id "
                "AND p.conversation_id = g.conversation_id AND p.status = 'pending' "
                "AND (p.created_at, p.id) < (g.created_at, g.id)))) "
                "ORDER BY g.created_at, g.id LIMIT %(limit)s",
                {
                    "after_time": after[0] if after else None,
                    "after_id": after[1] if after else None,
                    "limit": limit + 1,
                },
            )
        messages = [self.record_factory(row) for row in rows[:limit]]
        last = messages[-1] if len(rows) > limit else None
        cursor = (last.created_at, last.id) if last is not None and last.created_at is not None else None
        return messages, cursor

    async def delivered_dispatch_page(
        self, limit: int, *, after: tuple[datetime, UUID] | None = None
    ) -> tuple[list[GatewayMessageRecord], tuple[datetime, UUID] | None]:
        """Page delivered rows whose reply binding still belongs to the agent's current owner and tenant."""
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list('g')} FROM gateway_messages g "
                "JOIN agents a ON a.id = g.agent_id "
                "JOIN openclaw_reply_bindings b ON b.id = g.reply_binding_id AND b.agent_id = g.agent_id "
                "AND b.owner_id = a.creator_id AND b.tenant_id IS NOT DISTINCT FROM a.tenant_id "
                "WHERE g.status = 'delivered' AND (%(after_time)s::timestamptz IS NULL "
                "OR (g.created_at, g.id) > (%(after_time)s, %(after_id)s)) "
                "ORDER BY g.created_at, g.id LIMIT %(limit)s",
                {
                    "after_time": after[0] if after else None,
                    "after_id": after[1] if after else None,
                    "limit": limit + 1,
                },
            )
        messages = [self.record_factory(row) for row in rows[:limit]]
        last = messages[-1] if len(rows) > limit else None
        cursor = (last.created_at, last.id) if last is not None and last.created_at is not None else None
        return messages, cursor

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

    async def complete(
        self, message_id: UUID, agent_id: UUID, *, result: str, now: datetime, binding_id: UUID | None = None
    ) -> GatewayMessageRecord | None:
        """Record the first report for a row; ``None`` when it is unknown or already completed."""
        async with self.session() as db:
            row = await db.fetchone(
                "UPDATE gateway_messages SET status = 'completed', result = %(result)s, completed_at = %(now)s "
                "WHERE id = %(id)s AND agent_id = %(agent_id)s AND status IN ('pending', 'delivered') "
                "AND (%(binding_id)s::uuid IS NULL OR reply_binding_id = %(binding_id)s) "
                f"RETURNING {self._select_list()}",
                {"id": message_id, "agent_id": agent_id, "result": result, "now": now, "binding_id": binding_id},
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


gateway_message_dao = GatewayMessageDAO()
