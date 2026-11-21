from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from app.dao.base import BaseDAO
from app.records.gateway_message import GatewayMessageRecord

# Below every row: unlike ``after IS NULL OR``, a row bound stays an index condition when the plan is generic.
_DISPATCH_START: Final = (datetime.min.replace(tzinfo=UTC), UUID(int=0))


class GatewayMessageDeliveryDAO(BaseDAO[GatewayMessageRecord]):
    async def delivered_dispatch_page(
        self, limit: int, *, after: tuple[datetime, UUID] | None = None
    ) -> tuple[list[GatewayMessageRecord], tuple[datetime, UUID] | None]:
        """Page delivered rows whose reply binding still belongs to the agent's current owner and tenant."""
        after_time, after_id = after or _DISPATCH_START
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list('g')} FROM gateway_messages g "
                "JOIN agents a ON a.id = g.agent_id "
                "JOIN openclaw_reply_bindings b ON b.id = g.reply_binding_id AND b.agent_id = g.agent_id "
                "AND b.owner_id = a.creator_id AND b.tenant_id IS NOT DISTINCT FROM a.tenant_id "
                "WHERE g.status = 'delivered' AND g.reply_binding_id IS NOT NULL "
                "AND (g.created_at, g.id) > (%(after_time)s, %(after_id)s) "
                "ORDER BY g.created_at, g.id LIMIT %(limit)s",
                {"after_time": after_time, "after_id": after_id, "limit": limit + 1},
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

    async def complete_recovered(
        self, claimed: GatewayMessageRecord, result: str, now: datetime
    ) -> GatewayMessageRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                "UPDATE gateway_messages SET status = 'completed', result = %(result)s, completed_at = %(now)s "
                "WHERE id = %(id)s AND agent_id = %(agent)s AND status = 'delivered' "
                "AND delivered_at = %(delivered)s AND reply_binding_id = %(binding)s "
                f"RETURNING {self._select_list()}",
                {
                    "id": claimed.id,
                    "agent": claimed.agent_id,
                    "delivered": claimed.delivered_at,
                    "binding": claimed.reply_binding_id,
                    "result": result,
                    "now": now,
                },
            )
            return GatewayMessageRecord.from_row(row) if row is not None else None
