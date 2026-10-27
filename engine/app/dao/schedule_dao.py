"""DAO for agent_schedules"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import ClassVar, final, override
from uuid import UUID, uuid4

from app.core.logging import logger
from app.dao.base import BaseDAO
from app.records.schedule import AgentScheduleRecord

_COLUMNS = (
    "id",
    "agent_id",
    "name",
    "instruction",
    "cron_expr",
    "timezone",
    "is_enabled",
    "notify_on_failure",
    "last_run_at",
    "next_run_at",
    "run_count",
    "created_by",
    "created_at",
    "pending_due_at",
    "pending_instruction",
    "pending_notify_on_failure",
    "lease_token",
    "lease_expires_at",
    "pending_started_at",
)
_SLOT_COLUMNS = (
    "pending_due_at",
    "pending_instruction",
    "lease_token",
    "lease_expires_at",
    "pending_started_at",
    "pending_notify_on_failure",
)
_CLEAR_SLOT = ", ".join(f"{column} = NULL" for column in _SLOT_COLUMNS)
_OWNER_PREDICATE = (
    "id = %(id)s AND is_enabled IS TRUE AND pending_due_at IS NOT NULL "
    "AND lease_token = %(token)s AND lease_expires_at > %(now)s"
)
_LEASE_DURATION = timedelta(minutes=5)


@final
class AgentScheduleDAO(BaseDAO[AgentScheduleRecord]):
    table: ClassVar[str] = "agent_schedules"
    columns: ClassVar[tuple[str, ...]] = _COLUMNS
    record_factory = staticmethod(AgentScheduleRecord.from_row)

    @override
    async def update(self, *, db_obj: AgentScheduleRecord, obj_in: Mapping[str, object]) -> AgentScheduleRecord:
        values = dict(obj_in)
        if values.get("is_enabled") is False:
            values.update(dict.fromkeys(("next_run_at", *_SLOT_COLUMNS)))
        return await super().update(db_obj=db_obj, obj_in=values)

    async def list_for_agent(self, agent_id: UUID) -> Sequence[AgentScheduleRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM agent_schedules "
                "WHERE agent_id = %(agent_id)s ORDER BY created_at DESC",
                {"agent_id": agent_id},
            )
            return [AgentScheduleRecord.from_row(row) for row in rows]

    async def get_for_agent(self, schedule_id: UUID, agent_id: UUID) -> AgentScheduleRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM agent_schedules WHERE id = %(id)s AND agent_id = %(agent_id)s",
                {"id": schedule_id, "agent_id": agent_id},
            )
            return AgentScheduleRecord.from_row(row) if row is not None else None

    async def list_due(self, now: datetime) -> Sequence[AgentScheduleRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM agent_schedules "
                "WHERE is_enabled IS TRUE AND next_run_at <= %(now)s",
                {"now": now},
            )
            return [AgentScheduleRecord.from_row(row) for row in rows]

    async def claim_due(
        self, now: datetime, compute_next_run: Callable[[str, datetime, str], datetime | None]
    ) -> Sequence[AgentScheduleRecord]:
        """Persist a fresh occurrence or reclaim its expired lease under the row lock."""
        claimed: list[AgentScheduleRecord] = []
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM agent_schedules "
                "WHERE is_enabled IS TRUE AND ("
                "(pending_due_at IS NULL AND next_run_at <= %(now)s) OR "
                "(pending_due_at IS NOT NULL AND lease_expires_at <= %(now)s)) FOR UPDATE SKIP LOCKED",
                {"now": now},
            )
            for row in rows:
                schedule = AgentScheduleRecord.from_row(row)
                values: dict[str, object] = {
                    "id": schedule.id,
                    "lease_token": uuid4(),
                    "lease_expires_at": now + _LEASE_DURATION,
                }
                assignments = "lease_token = %(lease_token)s, lease_expires_at = %(lease_expires_at)s"
                if schedule.pending_due_at is None:
                    next_run = compute_next_run(schedule.cron_expr, now, schedule.timezone)
                    if next_run is None:
                        await db.execute(
                            f"UPDATE agent_schedules SET is_enabled = FALSE, next_run_at = NULL, {_CLEAR_SLOT} "
                            "WHERE id = %(id)s",
                            {"id": schedule.id},
                        )
                        logger.bind(schedule_id=str(schedule.id)).warning(
                            "Disabled schedule with invalid cron expression"
                        )
                        continue
                    assignments += (
                        ", pending_due_at = next_run_at, pending_instruction = instruction, "
                        "pending_notify_on_failure = notify_on_failure, pending_started_at = NULL, "
                        "next_run_at = %(next_run_at)s"
                    )
                    values["next_run_at"] = next_run
                claimed_row = await db.fetchone(
                    f"UPDATE agent_schedules SET {assignments} WHERE id = %(id)s RETURNING {self._select_list()}",
                    values,
                )
                if claimed_row is not None:
                    claimed.append(AgentScheduleRecord.from_row(claimed_row))
        return claimed

    async def renew_occurrence(self, schedule_id: UUID, token: UUID, now: datetime) -> bool:
        async with self.session() as db:
            row = await db.fetchone(
                "UPDATE agent_schedules SET lease_expires_at = %(lease_expires_at)s "
                f"WHERE {_OWNER_PREDICATE} RETURNING id",
                {"id": schedule_id, "token": token, "now": now, "lease_expires_at": now + _LEASE_DURATION},
            )
            return row is not None

    async def start_occurrence(self, schedule_id: UUID, token: UUID, now: datetime) -> bool:
        """Count the first eligible start; an already-counted live owner remains valid."""
        async with self.session() as db:
            row = await db.fetchone(
                "UPDATE agent_schedules SET "
                "run_count = CASE WHEN pending_started_at IS NULL THEN COALESCE(run_count, 0) + 1 ELSE run_count END, "
                "last_run_at = CASE WHEN pending_started_at IS NULL THEN %(now)s ELSE last_run_at END, "
                "pending_started_at = COALESCE(pending_started_at, %(now)s) "
                f"WHERE {_OWNER_PREDICATE} RETURNING id",
                {"id": schedule_id, "token": token, "now": now},
            )
            return row is not None

    async def finish_occurrence(self, schedule_id: UUID, token: UUID, now: datetime) -> bool:
        async with self.session() as db:
            row = await db.fetchone(
                f"UPDATE agent_schedules SET {_CLEAR_SLOT} WHERE {_OWNER_PREDICATE} RETURNING id",
                {"id": schedule_id, "token": token, "now": now},
            )
            return row is not None

    async def record_started(self, schedule_id: UUID, now: datetime) -> None:
        """Count a run only after execution passes its eligibility checks."""
        async with self.session() as db:
            await db.execute(
                "UPDATE agent_schedules SET run_count = COALESCE(run_count, 0) + 1, last_run_at = %(now)s "
                "WHERE id = %(id)s",
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
                f"UPDATE agent_schedules SET is_enabled = FALSE, next_run_at = NULL, {_CLEAR_SLOT} "
                "WHERE is_enabled IS TRUE AND agent_id IN ("
                "SELECT id FROM agents WHERE tenant_id = %(tenant_id)s"
                ") RETURNING id",
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
                f"UPDATE agent_schedules SET is_enabled = FALSE, next_run_at = NULL, {_CLEAR_SLOT} "
                "WHERE is_enabled IS TRUE AND agent_id IN ("
                f"SELECT id FROM agents WHERE {agent_filter}"
                ") RETURNING id",
                params,
            )
            return len(rows)


agent_schedule_dao = AgentScheduleDAO()
