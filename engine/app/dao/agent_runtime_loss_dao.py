from uuid import UUID

from app.db.connection import DbConnection
from app.records.agent_runtime_loss import AgentRuntimeLoss, RuntimeLossReason

_COLUMNS = "agent_id,reason,recorded_container_id,image,detected_at"


class AgentRuntimeLossDAO:
    async def record(
        self,
        db: DbConnection,
        agent_id: UUID,
        reason: RuntimeLossReason,
        *,
        recorded_container_id: str | None,
        image: str | None,
    ) -> AgentRuntimeLoss:
        """Upsert; a re-report keeps the first detection and notification, a new reason replaces reason/image."""
        row = await db.fetchone(
            "INSERT INTO agent_runtime_losses AS l (agent_id,reason,recorded_container_id,image) "
            "VALUES (%(id)s,%(reason)s,%(container)s,%(image)s) "
            "ON CONFLICT (agent_id) DO UPDATE SET "
            "image=CASE WHEN l.reason=EXCLUDED.reason THEN COALESCE(EXCLUDED.image,l.image) ELSE EXCLUDED.image END,"
            "reason=EXCLUDED.reason,"
            "recorded_container_id=COALESCE(l.recorded_container_id,EXCLUDED.recorded_container_id) "
            f"RETURNING {_COLUMNS}",
            {"id": agent_id, "reason": reason, "container": recorded_container_id, "image": image},
        )
        if row is None:
            raise RuntimeError("agent_runtime_losses upsert returned no row")
        return AgentRuntimeLoss.from_row(row)

    async def get(self, db: DbConnection, agent_id: UUID) -> AgentRuntimeLoss | None:
        row = await db.fetchone(f"SELECT {_COLUMNS} FROM agent_runtime_losses WHERE agent_id=%(id)s", {"id": agent_id})
        return AgentRuntimeLoss.from_row(row) if row is not None else None

    async def clear(self, db: DbConnection, agent_id: UUID) -> None:
        await db.execute("DELETE FROM agent_runtime_losses WHERE agent_id=%(id)s", {"id": agent_id})

    async def claim_notification(self, db: DbConnection, agent_id: UUID) -> bool:
        """Exactly one caller across workers wins the right to alert for the current loss."""
        claimed = await db.fetchval(
            "UPDATE agent_runtime_losses SET notified_at=now() "
            "WHERE agent_id=%(id)s AND notified_at IS NULL RETURNING agent_id",
            {"id": agent_id},
        )
        return claimed is not None

    async def pending_messages(self, db: DbConnection, agent_id: UUID) -> int:
        count = await db.fetchval(
            "SELECT count(*) FROM gateway_messages WHERE agent_id=%(id)s AND status IN ('pending','delivered')",
            {"id": agent_id},
        )
        return count if isinstance(count, int) else 0

    async def unpin_image(self, db: DbConnection, agent_id: UUID, image: str) -> bool:
        """Clear the agent's image pin only while it still names ``image``."""
        cleared = await db.fetchval(
            "UPDATE agents SET openclaw_image_id=NULL WHERE id=%(id)s AND openclaw_image_id=%(image)s RETURNING id",
            {"id": agent_id, "image": image},
        )
        return cleared is not None

    async def repin_image(self, db: DbConnection, agent_id: UUID, image: str) -> bool:
        """Restore a pin cleared by ``unpin_image`` unless something has pinned another image since."""
        pinned = await db.fetchval(
            "UPDATE agents SET openclaw_image_id=%(image)s WHERE id=%(id)s AND openclaw_image_id IS NULL RETURNING id",
            {"id": agent_id, "image": image},
        )
        return pinned is not None


agent_runtime_loss_dao = AgentRuntimeLossDAO()
