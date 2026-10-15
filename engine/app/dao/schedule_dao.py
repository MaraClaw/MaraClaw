"""DAO for agent_schedules"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import ClassVar, final
from uuid import UUID

from app.core.logging import logger
from app.dao.base import BaseDAO
from app.records.schedule import AgentScheduleRecord

_COLUMNS = (
    "id",
    "agent_id",
    "name",
    "instruction",
    "cron_expr",
    "is_enabled",
    "last_run_at",
    "next_run_at",
    "run_count",
    "created_by",
    "created_at",
)


@final
class AgentScheduleDAO(BaseDAO[AgentScheduleRecord]):
    table: ClassVar[str] = "agent_schedules"
    columns: ClassVar[tuple[str, ...]] = _COLUMNS
    record_factory = staticmethod(AgentScheduleRecord.from_row)

    async def list_for_agent(self, agent_id: UUID) -> Sequence[AgentScheduleRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM agent_schedules "
                + "WHERE agent_id = %(agent_id)s ORDER BY created_at DESC",
                {"agent_id": agent_id},
            )
            return [AgentScheduleRecord.from_row(row) for row in rows]

    async def get_for_agent(self, schedule_id: UUID, agent_id: UUID) -> AgentScheduleRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM agent_schedules WHERE id = %(id)s AND agent_id = %(agent_id)s",
                {"id": schedule_id, "agent_id": agent_id},
            )
            return AgentScheduleRecord.from_row(row) if row else None

    async def list_due(self, now: datetime) -> Sequence[AgentScheduleRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM agent_schedules "
                + "WHERE is_enabled IS TRUE AND next_run_at <= %(now)s",
                {"now": now},
            )
            return [AgentScheduleRecord.from_row(row) for row in rows]

    async def claim_due(
        self, now: datetime, compute_next_run: Callable[[str, datetime], datetime | None]
    ) -> Sequence[AgentScheduleRecord]:
        """Claim each missed schedule once, advancing its slot before releasing row locks."""
        claimed: list[AgentScheduleRecord] = []
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM agent_schedules "
                + "WHERE is_enabled IS TRUE AND next_run_at <= %(now)s FOR UPDATE SKIP LOCKED",
                {"now": now},
            )
            for row in rows:
                schedule = AgentScheduleRecord.from_row(row)
                next_run = compute_next_run(schedule.cron_expr, now)
                if next_run is None:
                    await db.execute(
                        "UPDATE agent_schedules SET is_enabled = FALSE, next_run_at = NULL WHERE id = %(id)s",
                        {"id": schedule.id},
                    )
                    logger.bind(schedule_id=str(schedule.id)).warning("Disabled schedule with invalid cron expression")
                    continue
                await db.execute(
                    "UPDATE agent_schedules SET next_run_at = %(next_run_at)s WHERE id = %(id)s",
                    {"id": schedule.id, "next_run_at": next_run},
                )
                schedule.next_run_at = next_run
                claimed.append(schedule)
        return claimed

    async def record_started(self, schedule_id: UUID, now: datetime) -> None:
        """Count a run only after execution passes its eligibility checks."""
        async with self.session() as db:
            await db.execute(
                "UPDATE agent_schedules SET run_count = COALESCE(run_count, 0) + 1, last_run_at = %(now)s "
                + "WHERE id = %(id)s",
                {"id": schedule_id, "now": now},
            )

    async def list_all(self) -> Sequence[AgentScheduleRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM agent_schedules ORDER BY created_at",
            )
            return [AgentScheduleRecord.from_row(row) for row in rows]

    async def disable_for_tenant(self, tenant_id: UUID) -> int:
        async with self.session() as db:
            rows = await db.fetchall(
                "UPDATE agent_schedules SET is_enabled = FALSE, next_run_at = NULL "
                + "WHERE is_enabled IS TRUE AND agent_id IN ("
                + "SELECT id FROM agents WHERE tenant_id = %(tenant_id)s"
                + ") RETURNING id",
                {"tenant_id": tenant_id},
            )
            return len(rows)

    async def disable_for_creator(self, creator_id: UUID, *, tenant_id: UUID | None = None) -> int:
        agent_filter = "creator_id = %(creator_id)s"
        params: dict[str, object] = {"creator_id": creator_id}
        if tenant_id is not None:
            agent_filter += " AND tenant_id = %(tenant_id)s"
            params["tenant_id"] = tenant_id
        async with self.session() as db:
            rows = await db.fetchall(
                "UPDATE agent_schedules SET is_enabled = FALSE, next_run_at = NULL "
                + "WHERE is_enabled IS TRUE AND agent_id IN ("
                + f"SELECT id FROM agents WHERE {agent_filter}"
                + ") RETURNING id",
                params,
            )
            return len(rows)


agent_schedule_dao = AgentScheduleDAO()
