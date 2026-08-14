"""DAO for agent_triggers and trigger_executions (psycopg)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import ClassVar, final
from uuid import UUID

from app.core.json_types import int_from_row
from app.dao.base import BaseDAO
from app.db.errors import UniqueViolationError
from app.records.trigger import AgentTriggerRecord, TriggerExecutionRecord

_TRIGGER_COLUMNS = (
    "id",
    "agent_id",
    "name",
    "type",
    "config",
    "reason",
    "focus_ref",
    "is_enabled",
    "last_fired_at",
    "fire_count",
    "max_fires",
    "cooldown_seconds",
    "is_system",
    "created_at",
    "expires_at",
)

_EXECUTION_COLUMNS = (
    "id",
    "trigger_id",
    "agent_id",
    "source",
    "status",
    "idempotency_key",
    "payload",
    "payload_text",
    "lease_owner",
    "lease_expires_at",
    "scheduled_at",
    "started_at",
    "finished_at",
    "last_error",
    "created_at",
)


@final
class AgentTriggerDAO(BaseDAO[AgentTriggerRecord]):
    """DAO for agent trigger definitions."""

    table: ClassVar[str] = "agent_triggers"
    columns: ClassVar[tuple[str, ...]] = _TRIGGER_COLUMNS
    record_factory = staticmethod(AgentTriggerRecord.from_row)

    async def list_for_agent(self, agent_id: UUID) -> list[AgentTriggerRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM agent_triggers "
                + "WHERE agent_id = %(agent_id)s ORDER BY created_at DESC",
                {"agent_id": agent_id},
            )
            return [AgentTriggerRecord.from_row(row) for row in rows]

    async def get_for_agent(self, trigger_id: UUID, agent_id: UUID) -> AgentTriggerRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM agent_triggers WHERE id = %(id)s AND agent_id = %(agent_id)s",
                {"id": trigger_id, "agent_id": agent_id},
            )
            return AgentTriggerRecord.from_row(row) if row else None

    async def list_enabled_by_type(self, trigger_type: str) -> list[AgentTriggerRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM agent_triggers WHERE type = %(type)s AND is_enabled IS TRUE",
                {"type": trigger_type},
            )
            return [AgentTriggerRecord.from_row(row) for row in rows]

    async def list_enabled(self) -> list[AgentTriggerRecord]:
        async with self.session() as db:
            rows = await db.fetchall(f"SELECT {self._select_list()} FROM agent_triggers WHERE is_enabled IS TRUE")
            return [AgentTriggerRecord.from_row(row) for row in rows]

    async def find_webhook_by_token(self, token: str) -> AgentTriggerRecord | None:
        """Find an enabled webhook trigger whose config.token matches."""
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM agent_triggers "
                + "WHERE type = 'webhook' AND is_enabled IS TRUE "
                + "AND config->>'token' = %(token)s LIMIT 1",
                {"token": token},
            )
            return AgentTriggerRecord.from_row(row) if row else None

    async def get_by_agent_and_name(self, agent_id: UUID, name: str) -> AgentTriggerRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM agent_triggers "
                + "WHERE agent_id = %(agent_id)s AND name = %(name)s LIMIT 1",
                {"agent_id": agent_id, "name": name},
            )
            return AgentTriggerRecord.from_row(row) if row else None

    async def count_enabled_for_agent(self, agent_id: UUID) -> int:
        async with self.session() as db:
            value = await db.fetchval(
                "SELECT COUNT(*) FROM agent_triggers WHERE agent_id = %(agent_id)s AND is_enabled IS TRUE",
                {"agent_id": agent_id},
            )
            return int_from_row(value)

    async def disable_for_tenant(self, tenant_id: UUID) -> int:
        async with self.session() as db:
            rows = await db.fetchall(
                "UPDATE agent_triggers SET is_enabled = FALSE "
                + "WHERE is_enabled IS TRUE AND agent_id IN ("
                + "SELECT id FROM agents WHERE tenant_id = %(tenant_id)s"
                + ") RETURNING id",
                {"tenant_id": tenant_id},
            )
            return len(rows)


@final
class TriggerExecutionDAO(BaseDAO[TriggerExecutionRecord]):
    """DAO for durable trigger execution queue rows."""

    table: ClassVar[str] = "trigger_executions"
    columns: ClassVar[tuple[str, ...]] = _EXECUTION_COLUMNS
    record_factory = staticmethod(TriggerExecutionRecord.from_row)

    async def try_enqueue(self, *, obj_in: Mapping[str, object]) -> tuple[TriggerExecutionRecord | None, bool]:
        """Insert an execution row; return (None, False) on idempotency conflict.

        Uses a savepoint so a unique violation does not abort an outer request
        transaction (mirrors the old IntegrityError + rollback pattern).
        """
        from uuid import uuid4 as _uuid4

        from app.db.types import as_jsonb

        data = dict(obj_in)
        if self.pk not in data and self.pk == "id":
            data[self.pk] = _uuid4()
        cols = list(dict.fromkeys([c for c in data if c in self.columns or c == self.pk]))
        if not cols:
            raise ValueError("try_enqueue() requires at least one column value")
        params: dict[str, object] = {}
        for col in cols:
            value: object = data[col]
            params[col] = as_jsonb(value) if isinstance(value, dict) else value
        col_sql = ", ".join(cols)
        val_sql = ", ".join(f"%({c})s" for c in cols)
        sql = f"INSERT INTO {self.table} ({col_sql}) VALUES ({val_sql}) RETURNING {self._select_list()}"

        async with self.session() as db:
            try:
                async with db.raw.transaction():
                    row = await db.fetchone(sql, params)
                    if row is None:
                        raise RuntimeError(f"INSERT into {self.table} returned no row")
                    return TriggerExecutionRecord.from_row(row), True
            except UniqueViolationError:
                return None, False

    async def claim_pending(
        self,
        *,
        sources: list[str],
        now: datetime,
        limit: int,
    ) -> list[tuple[TriggerExecutionRecord, AgentTriggerRecord]]:
        """Claim pending/expired-lease executions with FOR UPDATE SKIP LOCKED."""
        exec_cols = ", ".join(f"e.{c} AS e_{c}" for c in self.columns)
        trig_cols = ", ".join(f"t.{c} AS t_{c}" for c in agent_trigger_dao.columns)
        async with self.session() as db:
            rows = await db.fetchall(
                f"""
                SELECT {exec_cols}, {trig_cols}
                FROM trigger_executions e
                JOIN agent_triggers t ON t.id = e.trigger_id
                WHERE e.source = ANY(%(sources)s)
                  AND t.is_enabled IS TRUE
                  AND (
                        e.status = 'pending'
                        OR (
                            e.status = 'processing'
                            AND (e.lease_expires_at IS NULL OR e.lease_expires_at < %(now)s)
                        )
                  )
                ORDER BY e.scheduled_at ASC
                FOR UPDATE OF e SKIP LOCKED
                LIMIT %(limit)s
                """,
                {"sources": sources, "now": now, "limit": limit},
            )
            pairs: list[tuple[TriggerExecutionRecord, AgentTriggerRecord]] = []
            for row in rows:
                exec_row = {col: row[f"e_{col}"] for col in self.columns}
                trig_row = {col: row[f"t_{col}"] for col in agent_trigger_dao.columns}
                pairs.append((TriggerExecutionRecord.from_row(exec_row), AgentTriggerRecord.from_row(trig_row)))
            return pairs


# Instantiated after class bodies so claim_pending can reference agent_trigger_dao columns.
agent_trigger_dao = AgentTriggerDAO()
trigger_execution_dao = TriggerExecutionDAO()
