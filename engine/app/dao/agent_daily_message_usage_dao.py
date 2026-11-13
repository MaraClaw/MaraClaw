from collections.abc import Sequence
from datetime import date
from typing import ClassVar, final
from uuid import UUID

from app.core.json_types import int_from_row, uuid_from_row
from app.dao.base import BaseDAO
from app.records.agent_daily_message_usage import AgentDailyMessageUsageRecord


@final
class AgentDailyMessageUsageDAO(BaseDAO[AgentDailyMessageUsageRecord]):
    """One counter row per agent. The row's ``usage_date`` is the local day it counts; any other date reads as zero."""

    table: ClassVar[str] = "agent_daily_message_usage"
    pk: ClassVar[str] = "agent_id"
    columns: ClassVar[tuple[str, ...]] = ("agent_id", "usage_date", "messages_used", "updated_at")
    record_factory = staticmethod(AgentDailyMessageUsageRecord.from_row)

    async def charge(self, agent_id: UUID, usage_date: date, limit: int) -> int | None:
        """Admit one message for ``usage_date`` unless that day already holds ``limit``; ``None`` means refused.

        One statement: concurrent charges serialize on the row, and the ``WHERE`` is re-evaluated against the winner's
        committed value, so the cap cannot be exceeded. A row left by another date restarts the count at one.
        """
        async with self.session() as db:
            row = await db.fetchone(
                f"""
                INSERT INTO {self.table} AS usage (agent_id, usage_date, messages_used)
                VALUES (%(agent_id)s, %(usage_date)s, 1)
                ON CONFLICT (agent_id) DO UPDATE
                SET usage_date = EXCLUDED.usage_date,
                    messages_used = CASE WHEN usage.usage_date = EXCLUDED.usage_date
                                         THEN usage.messages_used + 1 ELSE 1 END,
                    updated_at = now()
                WHERE usage.usage_date <> EXCLUDED.usage_date OR usage.messages_used < %(limit)s
                RETURNING messages_used
                """,
                {"agent_id": agent_id, "usage_date": usage_date, "limit": limit},
            )
            return int_from_row(row["messages_used"]) if row is not None else None

    async def used_on(self, agent_ids: Sequence[UUID], usage_date: date) -> dict[UUID, int]:
        """Messages counted on ``usage_date`` for each agent that has any; absent agents used none."""
        if not agent_ids:
            return {}
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT agent_id, messages_used FROM {self.table} "
                "WHERE agent_id = ANY(%(ids)s) AND usage_date = %(usage_date)s",
                {"ids": list(agent_ids), "usage_date": usage_date},
            )
            return {uuid_from_row(row["agent_id"]): int_from_row(row["messages_used"]) for row in rows}

    async def reset(self, agent_id: UUID, usage_date: date) -> int:
        """Zero ``usage_date``'s count and return what it held; no row for that day means nothing to reset (0)."""
        async with self.session() as db:
            row = await db.fetchone(
                f"""
                WITH prior AS (
                    SELECT messages_used FROM {self.table}
                    WHERE agent_id = %(agent_id)s AND usage_date = %(usage_date)s FOR UPDATE
                )
                UPDATE {self.table} AS usage SET messages_used = 0, updated_at = now()
                FROM prior WHERE usage.agent_id = %(agent_id)s
                RETURNING prior.messages_used AS before_used
                """,
                {"agent_id": agent_id, "usage_date": usage_date},
            )
            return int_from_row(row["before_used"]) if row is not None else 0


agent_daily_message_usage_dao = AgentDailyMessageUsageDAO()
