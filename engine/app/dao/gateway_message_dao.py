"""DAO for gateway_messages (psycopg)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar, final
from uuid import UUID

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

    async def get_for_agent(self, message_id: UUID, agent_id: UUID) -> GatewayMessageRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM gateway_messages "
                + "WHERE id = %(id)s AND agent_id = %(agent_id)s LIMIT 1",
                {"id": message_id, "agent_id": agent_id},
            )
            return GatewayMessageRecord.from_row(row) if row else None

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
