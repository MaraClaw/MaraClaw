"""DAO for agent_activity_logs"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any, ClassVar, final
from uuid import UUID

from app.core.json_types import date_from_row, int_from_row
from app.dao.base import BaseDAO
from app.records.activity_log import AgentActivityLogRecord

_COLUMNS = (
    "id",
    "agent_id",
    "action_type",
    "summary",
    "detail_json",
    "related_id",
    "created_at",
)


@final
class AgentActivityLogDAO(BaseDAO[AgentActivityLogRecord]):
    table: ClassVar[str] = "agent_activity_logs"
    columns: ClassVar[tuple[str, ...]] = _COLUMNS
    record_factory = staticmethod(AgentActivityLogRecord.from_row)

    async def list_for_agent(
        self,
        agent_id: UUID,
        *,
        action_type: str | None = None,
        action_types: Sequence[str] | None = None,
        limit: int = 50,
    ) -> Sequence[AgentActivityLogRecord]:
        params: dict[str, Any] = {"agent_id": agent_id, "limit": limit}
        clauses = ["agent_id = %(agent_id)s"]
        if action_type:
            clauses.append("action_type = %(action_type)s")
            params["action_type"] = action_type
        if action_types:
            clauses.append("action_type = ANY(%(action_types)s)")
            params["action_types"] = list(action_types)
        where = " AND ".join(clauses)
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM agent_activity_logs "
                + f"WHERE {where} ORDER BY created_at DESC LIMIT %(limit)s",
                params,
            )
            return [AgentActivityLogRecord.from_row(row) for row in rows]

    async def tokens_by_day(self, start: datetime | date, end: datetime | date) -> dict[date, int]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT DATE(date) AS d, COALESCE(SUM(tokens_used), 0) AS c "
                + "FROM daily_token_usage WHERE date >= %(start)s AND date <= %(end)s GROUP BY d",
                {"start": start, "end": end},
            )
            return {date_from_row(row["d"]): int_from_row(row.get("c")) for row in rows}

    async def cache_read_by_day(self, start: datetime | date, end: datetime | date) -> dict[date, int]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT DATE(date) AS d, COALESCE(SUM(cache_read_tokens), 0) AS c "
                + "FROM daily_token_usage WHERE date >= %(start)s AND date <= %(end)s GROUP BY d",
                {"start": start, "end": end},
            )
            return {date_from_row(row["d"]): int_from_row(row.get("c")) for row in rows}

    async def sum_tokens_since(self, since: datetime | date) -> int:
        async with self.session() as db:
            value = await db.fetchval(
                "SELECT COALESCE(SUM(tokens_used), 0) FROM daily_token_usage WHERE date >= %(since)s",
                {"since": since},
            )
            return int_from_row(value)

    async def upsert_daily_token_usage(
        self,
        *,
        tenant_id: UUID,
        agent_id: UUID,
        date: datetime | date,
        tokens_used: int,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        estimated_tokens: int = 0,
    ) -> None:
        """Insert or accumulate a daily_token_usage row for one agent/day."""
        from uuid import uuid4

        async with self.session() as db:
            await db.execute(
                "INSERT INTO daily_token_usage ("
                + "id, tenant_id, agent_id, date, tokens_used, input_tokens, output_tokens, "
                + "cache_read_tokens, cache_creation_tokens, estimated_tokens"
                + ") VALUES ("
                + "%(id)s, %(tenant_id)s, %(agent_id)s, %(date)s, %(tokens_used)s, %(input_tokens)s, "
                + "%(output_tokens)s, %(cache_read_tokens)s, %(cache_creation_tokens)s, %(estimated_tokens)s"
                + ") ON CONFLICT (agent_id, date) DO UPDATE SET "
                + "tokens_used = daily_token_usage.tokens_used + EXCLUDED.tokens_used, "
                + "input_tokens = daily_token_usage.input_tokens + EXCLUDED.input_tokens, "
                + "output_tokens = daily_token_usage.output_tokens + EXCLUDED.output_tokens, "
                + "cache_read_tokens = daily_token_usage.cache_read_tokens + EXCLUDED.cache_read_tokens, "
                + "cache_creation_tokens = daily_token_usage.cache_creation_tokens + EXCLUDED.cache_creation_tokens, "
                + "estimated_tokens = daily_token_usage.estimated_tokens + EXCLUDED.estimated_tokens, "
                + "updated_at = NOW()",
                {
                    "id": uuid4(),
                    "tenant_id": tenant_id,
                    "agent_id": agent_id,
                    "date": date,
                    "tokens_used": tokens_used,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_read_tokens": cache_read_tokens,
                    "cache_creation_tokens": cache_creation_tokens,
                    "estimated_tokens": estimated_tokens,
                },
            )


agent_activity_log_dao = AgentActivityLogDAO()
