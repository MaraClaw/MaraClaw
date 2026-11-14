from datetime import datetime
from typing import ClassVar, final
from uuid import UUID

from app.dao.base import BaseDAO
from app.records.agent_workspace_usage import AgentWorkspaceUsageRecord


@final
class AgentWorkspaceUsageDAO(BaseDAO[AgentWorkspaceUsageRecord]):
    """One measurement row per agent; ``over_since`` is set exactly while the last measurement exceeds its limit."""

    table: ClassVar[str] = "agent_workspace_usage"
    pk: ClassVar[str] = "agent_id"
    columns: ClassVar[tuple[str, ...]] = (
        "agent_id",
        "dir_bytes",
        "layer_bytes",
        "limit_bytes",
        "measured_at",
        "over_since",
        "notified_at",
    )
    record_factory = staticmethod(AgentWorkspaceUsageRecord.from_row)

    async def record_measurement(
        self, agent_id: UUID, *, dir_bytes: int, layer_bytes: int, limit_bytes: int, started: datetime
    ) -> AgentWorkspaceUsageRecord | None:
        """Store one measurement and return the row; ``None`` when a measurement that started later is stored already.

        ``started`` is the timezone-aware moment the measurement began, taken before it read anything; it becomes
        ``measured_at``, so measurements are ordered by when they started rather than by when they land. One statement,
        so concurrent measurements serialize on the row, and a slower, older one cannot overwrite a newer one.

        ``over_since`` keeps its earlier value while the agent stays over and clears when it is not over. ``notified_at``
        survives only while over, so a later overage notifies again.
        """
        over = dir_bytes + layer_bytes > limit_bytes
        async with self.session() as db:
            row = await db.fetchone(
                f"""
                INSERT INTO {self.table} AS usage
                    (agent_id, dir_bytes, layer_bytes, limit_bytes, measured_at, over_since)
                VALUES (
                    %(agent_id)s, %(dir_bytes)s, %(layer_bytes)s, %(limit_bytes)s, %(started)s,
                    CASE WHEN %(over)s THEN %(started)s END
                )
                ON CONFLICT (agent_id) DO UPDATE
                SET dir_bytes = EXCLUDED.dir_bytes,
                    layer_bytes = EXCLUDED.layer_bytes,
                    limit_bytes = EXCLUDED.limit_bytes,
                    measured_at = EXCLUDED.measured_at,
                    over_since = CASE WHEN %(over)s THEN COALESCE(usage.over_since, EXCLUDED.measured_at) END,
                    notified_at = CASE WHEN %(over)s THEN usage.notified_at END
                WHERE usage.measured_at <= %(started)s
                RETURNING {self._select_list()}
                """,
                {
                    "agent_id": agent_id,
                    "dir_bytes": dir_bytes,
                    "layer_bytes": layer_bytes,
                    "limit_bytes": limit_bytes,
                    "started": started,
                    "over": over,
                },
            )
            return self._from_row(row) if row is not None else None

    async def claim_notification(self, agent_id: UUID) -> AgentWorkspaceUsageRecord | None:
        """Win the single alert of the current overage; ``None`` when not over or already claimed."""
        async with self.session() as db:
            row = await db.fetchone(
                f"""
                UPDATE {self.table} SET notified_at = now()
                WHERE agent_id = %(agent_id)s AND over_since IS NOT NULL AND notified_at IS NULL
                RETURNING {self._select_list()}
                """,
                {"agent_id": agent_id},
            )
            return self._from_row(row) if row is not None else None


agent_workspace_usage_dao = AgentWorkspaceUsageDAO()
