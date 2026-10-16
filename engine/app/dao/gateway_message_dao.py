"""DAO for gateway_messages"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import ClassVar, final
from uuid import UUID

from app.core.json_types import uuid_from_row
from app.dao.base import BaseDAO
from app.records.gateway_message import GatewayMessageRecord

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
                + "WHERE agent_id = %(agent_id)s AND status = 'pending' "
                + "ORDER BY created_at ASC",
                {"agent_id": agent_id},
            )
            return [GatewayMessageRecord.from_row(row) for row in rows]

    async def claim_pending(self, agent_id: UUID, *, now: datetime) -> list[GatewayMessageRecord]:
        """Move every pending row to delivered and return only the rows this call moved."""
        async with self.session() as db:
            rows = await db.fetchall(
                "WITH claimed AS ("
                + "UPDATE gateway_messages SET status = 'delivered', delivered_at = %(now)s "
                + "WHERE agent_id = %(agent_id)s AND status = 'pending' "
                + f"RETURNING {self._select_list()}"
                + f") SELECT {self._select_list()} FROM claimed ORDER BY created_at ASC",
                {"agent_id": agent_id, "now": now},
            )
            return [GatewayMessageRecord.from_row(row) for row in rows]

    async def mark_delivered(self, message_id: UUID, agent_id: UUID) -> GatewayMessageRecord | None:
        """Claim one pending row for a wake; ``None`` when a poll or another wake already took it."""
        async with self.session() as db:
            row = await db.fetchone(
                "UPDATE gateway_messages SET status = 'delivered', delivered_at = %(now)s "
                + "WHERE id = %(id)s AND agent_id = %(agent_id)s AND status = 'pending' "
                + f"RETURNING {self._select_list()}",
                {"id": message_id, "agent_id": agent_id, "now": datetime.now(UTC)},
            )
            return GatewayMessageRecord.from_row(row) if row is not None else None

    async def release_delivery(self, claimed: GatewayMessageRecord) -> GatewayMessageRecord | None:
        """Return a claimed row to pending, unless a newer claim or a report has moved it since."""
        async with self.session() as db:
            row = await db.fetchone(
                "UPDATE gateway_messages SET status = 'pending', delivered_at = NULL "
                + "WHERE id = %(id)s AND agent_id = %(agent_id)s AND status = 'delivered' "
                + "AND delivered_at = %(delivered_at)s "
                + f"RETURNING {self._select_list()}",
                {"id": claimed.id, "agent_id": claimed.agent_id, "delivered_at": claimed.delivered_at},
            )
            return GatewayMessageRecord.from_row(row) if row is not None else None

    async def requeue_stale_deliveries(self, agent_id: UUID, *, stale_before: datetime) -> list[UUID]:
        """Return delivered rows with no report since ``stale_before`` (or no ``delivered_at``) to pending."""
        async with self.session() as db:
            rows = await db.fetchall(
                "UPDATE gateway_messages SET status = 'pending', delivered_at = NULL "
                + "WHERE agent_id = %(agent_id)s AND status = 'delivered' "
                + "AND (delivered_at IS NULL OR delivered_at < %(stale_before)s) "
                + "RETURNING id",
                {"agent_id": agent_id, "stale_before": stale_before},
            )
            return [uuid_from_row(row["id"]) for row in rows]

    async def complete(
        self, message_id: UUID, agent_id: UUID, *, result: str, now: datetime
    ) -> GatewayMessageRecord | None:
        """Record the first report for a row; ``None`` when it is unknown or already completed."""
        async with self.session() as db:
            row = await db.fetchone(
                "UPDATE gateway_messages SET status = 'completed', result = %(result)s, completed_at = %(now)s "
                + "WHERE id = %(id)s AND agent_id = %(agent_id)s AND status IN ('pending', 'delivered') "
                + f"RETURNING {self._select_list()}",
                {"id": message_id, "agent_id": agent_id, "result": result, "now": now},
            )
            return GatewayMessageRecord.from_row(row) if row is not None else None

    async def get_for_agent(self, message_id: UUID, agent_id: UUID) -> GatewayMessageRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM gateway_messages "
                + "WHERE id = %(id)s AND agent_id = %(agent_id)s LIMIT 1",
                {"id": message_id, "agent_id": agent_id},
            )
            return GatewayMessageRecord.from_row(row) if row is not None else None

    async def list_recent(self, agent_id: UUID, *, limit: int = 50) -> Sequence[GatewayMessageRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM gateway_messages "
                + "WHERE agent_id = %(agent_id)s "
                + "ORDER BY created_at DESC NULLS LAST LIMIT %(limit)s",
                {"agent_id": agent_id, "limit": limit},
            )
            return [GatewayMessageRecord.from_row(row) for row in rows]


gateway_message_dao = GatewayMessageDAO()
