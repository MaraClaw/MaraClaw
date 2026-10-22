from datetime import datetime, timedelta
from typing import ClassVar, final
from uuid import UUID, uuid4

from app.dao.base import BaseDAO
from app.records.schedule_manual_run import ScheduleManualRunRecord

_LEASE_DURATION = timedelta(minutes=5)
_OWNER = "id = %(id)s AND finished_at IS NULL AND lease_token = %(token)s AND lease_expires_at > %(now)s"


@final
class ScheduleManualRunDAO(BaseDAO[ScheduleManualRunRecord]):
    table: ClassVar[str] = "agent_schedule_manual_runs"
    columns: ClassVar[tuple[str, ...]] = (
        "id",
        "schedule_id",
        "agent_id",
        "requester_id",
        "instruction",
        "idempotency_key",
        "created_at",
        "started_at",
        "finished_at",
        "outcome",
        "lease_token",
        "lease_expires_at",
    )
    record_factory = staticmethod(ScheduleManualRunRecord.from_row)

    async def get_scoped(self, run_id: UUID, schedule_id: UUID, agent_id: UUID) -> ScheduleManualRunRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM agent_schedule_manual_runs "
                "WHERE id = %(id)s AND schedule_id = %(schedule_id)s AND agent_id = %(agent_id)s",
                {"id": run_id, "schedule_id": schedule_id, "agent_id": agent_id},
            )
            return self.record_factory(row) if row is not None else None

    async def cancel_pending(self, run: ScheduleManualRunRecord, now: datetime) -> ScheduleManualRunRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                "UPDATE agent_schedule_manual_runs SET outcome = 'cancelled', finished_at = %(now)s, "
                "lease_token = NULL, lease_expires_at = NULL "
                "WHERE id = %(id)s AND schedule_id = %(schedule_id)s AND agent_id = %(agent_id)s "
                f"AND started_at IS NULL AND finished_at IS NULL RETURNING {self._select_list()}",
                {"id": run.id, "schedule_id": run.schedule_id, "agent_id": run.agent_id, "now": now},
            )
            return self.record_factory(row) if row is not None else None

    async def accept(
        self, schedule_id: UUID, requester_id: UUID, idempotency_key: str | None
    ) -> ScheduleManualRunRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                "INSERT INTO agent_schedule_manual_runs "
                "(id, schedule_id, agent_id, requester_id, instruction, idempotency_key) "
                "SELECT %(id)s, id, agent_id, %(requester_id)s, instruction, %(key)s "
                "FROM agent_schedules WHERE id = %(schedule_id)s "
                "ON CONFLICT (requester_id, schedule_id, idempotency_key) DO UPDATE "
                "SET idempotency_key = EXCLUDED.idempotency_key "
                f"RETURNING {self._select_list()}",
                {"id": uuid4(), "schedule_id": schedule_id, "requester_id": requester_id, "key": idempotency_key},
            )
            return self.record_factory(row) if row is not None else None

    async def claim_pending(self, now: datetime) -> list[ScheduleManualRunRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT id FROM agent_schedule_manual_runs WHERE finished_at IS NULL "
                "AND (lease_token IS NULL OR lease_expires_at <= %(now)s) "
                "ORDER BY created_at, id LIMIT 100 FOR UPDATE SKIP LOCKED",
                {"now": now},
            )
            claimed: list[ScheduleManualRunRecord] = []
            for candidate in rows:
                row = await db.fetchone(
                    "UPDATE agent_schedule_manual_runs SET lease_token = %(token)s, "
                    f"lease_expires_at = %(expires)s WHERE id = %(id)s RETURNING {self._select_list()}",
                    {"id": candidate["id"], "token": uuid4(), "expires": now + _LEASE_DURATION},
                )
                if row is not None:
                    claimed.append(self.record_factory(row))
            return claimed

    async def renew(self, run_id: UUID, token: UUID, now: datetime) -> bool:
        async with self.session() as db:
            row = await db.fetchone(
                f"UPDATE agent_schedule_manual_runs SET lease_expires_at = %(expires)s WHERE {_OWNER} RETURNING id",
                {"id": run_id, "token": token, "now": now, "expires": now + _LEASE_DURATION},
            )
            return row is not None

    async def start(self, run_id: UUID, token: UUID, now: datetime) -> bool:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT schedule_id, started_at FROM agent_schedule_manual_runs WHERE {_OWNER} FOR UPDATE",
                {"id": run_id, "token": token, "now": now},
            )
            if row is None:
                return False
            if row["started_at"] is None:
                await db.execute(
                    "UPDATE agent_schedule_manual_runs SET started_at = %(now)s WHERE id = %(id)s",
                    {"id": run_id, "now": now},
                )
                await db.execute(
                    "UPDATE agent_schedules SET run_count = COALESCE(run_count, 0) + 1, last_run_at = %(now)s "
                    "WHERE id = %(id)s",
                    {"id": row["schedule_id"], "now": now},
                )
            return True

    async def finish(self, run: ScheduleManualRunRecord, now: datetime, outcome: str) -> bool:
        async with self.session() as db:
            row = await db.fetchone(
                "UPDATE agent_schedule_manual_runs SET finished_at = %(now)s, outcome = %(outcome)s, "
                f"lease_token = NULL, lease_expires_at = NULL WHERE {_OWNER} RETURNING id",
                {"id": run.id, "token": run.lease_token, "now": now, "outcome": outcome},
            )
            return row is not None


schedule_manual_run_dao = ScheduleManualRunDAO()
