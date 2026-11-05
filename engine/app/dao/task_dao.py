"""DAO for tasks and task_logs"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import Any, ClassVar, final, override
from uuid import UUID, uuid4

from app.core.domain.tasks import TaskStatus, TaskType
from app.core.json_types import int_from_row, str_from_row, uuid_from_row
from app.dao.base import BaseDAO
from app.db.connection import DbConnection
from app.db.pool import get_pool
from app.db.session import transaction
from app.records.task import TaskClaim, TaskLogRecord, TaskRecord

_TASK_COLUMNS = (
    "id",
    "agent_id",
    "title",
    "description",
    "type",
    "status",
    "priority",
    "assignee",
    "created_by",
    "due_date",
    "supervision_target_user_id",
    "supervision_target_name",
    "supervision_channel",
    "remind_schedule",
    "created_at",
    "updated_at",
    "completed_at",
    "claim_token",
    "lease_expires_at",
)

_LOG_COLUMNS = ("id", "task_id", "content", "created_at")

# The lease clock is the database's, so engine processes with skewed clocks agree on expiry.
_LEASE_UNTIL = "clock_timestamp() + make_interval(secs => %(lease_seconds)s::float8)"

_CLAIM_COLUMNS = ("claim_token", "lease_expires_at")

# Settling is fenced on the claim token and writes its log entry in the same statement.
_SETTLE_DONE = """
    WITH settled AS (
        UPDATE tasks SET status = 'done', completed_at = clock_timestamp(),
               claim_token = NULL, lease_expires_at = NULL, updated_at = now()
        WHERE id = %(id)s AND claim_token = %(token)s
          AND lease_expires_at > clock_timestamp() RETURNING id
    ) INSERT INTO task_logs (id, task_id, content)
      SELECT gen_random_uuid(), id, %(content)s FROM settled RETURNING task_id"""
_SETTLE_PENDING = """
    WITH settled AS (
        UPDATE tasks SET status = 'pending', completed_at = NULL,
               claim_token = NULL, lease_expires_at = NULL, updated_at = now()
        WHERE id = %(id)s AND claim_token = %(token)s
          AND lease_expires_at > clock_timestamp() RETURNING id
    ) INSERT INTO task_logs (id, task_id, content)
      SELECT gen_random_uuid(), id, %(content)s FROM settled RETURNING task_id"""


@asynccontextmanager
async def _committed() -> AsyncGenerator[DbConnection]:
    """Own a checkout and commit even inside a request or a sibling task that holds a connection."""
    async with get_pool().connection() as raw, transaction(DbConnection(raw)) as db:
        yield db


@final
class TaskDAO(BaseDAO[TaskRecord]):
    table: ClassVar[str] = "tasks"
    columns: ClassVar[tuple[str, ...]] = _TASK_COLUMNS
    record_factory = staticmethod(TaskRecord.from_row)

    async def list_for_agent(
        self,
        agent_id: UUID,
        *,
        status: TaskStatus | None = None,
        task_type: TaskType | None = None,
        ascending: bool = False,
    ) -> Sequence[TaskRecord]:
        params: dict[str, Any] = {"agent_id": agent_id}
        clauses = ["agent_id = %(agent_id)s"]
        if status is not None:
            clauses.append("status = %(status)s")
            params["status"] = status.value
        if task_type is not None:
            clauses.append("type = %(type)s")
            params["type"] = task_type.value
        where = " AND ".join(clauses)
        order = "ASC" if ascending else "DESC"
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM tasks WHERE {where} ORDER BY created_at {order}",
                params,
            )
            return [TaskRecord.from_row(row) for row in rows]

    async def get_for_agent(self, task_id: UUID, agent_id: UUID) -> TaskRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM tasks WHERE id = %(id)s AND agent_id = %(agent_id)s",
                {"id": task_id, "agent_id": agent_id},
            )
            return TaskRecord.from_row(row) if row is not None else None

    async def count_for_agent(self, agent_id: UUID, *, status: TaskStatus | None = None) -> int:
        params: dict[str, Any] = {"agent_id": agent_id}
        status_sql = ""
        if status is not None:
            status_sql = " AND status = %(status)s"
            params["status"] = status.value
        async with self.session() as db:
            value = await db.fetchval(
                f"SELECT COUNT(*) FROM tasks WHERE agent_id = %(agent_id)s{status_sql}",
                params,
            )
            return int_from_row(value)

    async def list_active_supervision(self) -> Sequence[tuple[TaskRecord, str]]:
        """Return (task, agent_name) for active supervision tasks with a remind schedule."""
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list('t')}, a.name AS agent_name "
                "FROM tasks t JOIN agents a ON a.id = t.agent_id "
                "WHERE t.type = 'supervision' AND t.status = ANY(%(statuses)s) "
                "AND t.remind_schedule IS NOT NULL",
                {"statuses": ["pending", "doing"]},
            )
            return [
                (
                    TaskRecord.from_row({k: v for k, v in row.items() if k != "agent_name"}),
                    str_from_row(row.get("agent_name")),
                )
                for row in rows
            ]

    async def find_first_by_title_ilike(self, agent_id: UUID, title: str) -> TaskRecord | None:
        """Find the first task whose title contains ``title`` (case-insensitive)."""
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM tasks "
                "WHERE agent_id = %(agent_id)s AND title ILIKE %(pattern)s "
                "ORDER BY created_at DESC LIMIT 1",
                {"agent_id": agent_id, "pattern": f"%{title}%"},
            )
            return TaskRecord.from_row(row) if row is not None else None

    @override
    async def update(self, *, db_obj: TaskRecord, obj_in: Mapping[str, object]) -> TaskRecord:
        """Write the given columns; a changed status revokes the live claim in that same statement.

        Metadata-only edits and a status equal to the stored one leave the claim alone, so the run
        and its settle (fenced on the token) stay valid. The comparison reads the row, not ``db_obj``.
        """
        data = {k: v for k, v in obj_in.items() if k in self.columns and k not in (self.pk, *_CLAIM_COLUMNS)}
        if not data:
            return db_obj
        params: dict[str, object] = {self.pk: db_obj.id}
        assignments: list[str] = []
        for column, value in data.items():
            params[column] = value.value if isinstance(value, StrEnum) else value
            assignments.append(f"{column} = %({column})s")
        if "status" in data:
            assignments.extend(
                f"{column} = CASE WHEN status IS DISTINCT FROM %(status)s THEN NULL ELSE {column} END"
                for column in _CLAIM_COLUMNS
            )
        assignments.append("updated_at = now()")
        async with self.session() as db:
            row = await db.fetchone(
                f"UPDATE tasks SET {', '.join(assignments)} WHERE id = %(id)s RETURNING {self._select_list()}",
                params,
            )
        return TaskRecord.from_row(row) if row is not None else db_obj

    async def claim(self, task_id: UUID, agent_id: UUID, *, lease_seconds: float) -> TaskClaim | None:
        """Atomically start a run: ``doing`` plus a fresh owner token, unless an unexpired lease exists.

        One statement, so concurrent claims serialize on the row and exactly one wins. Joins an ambient
        transaction when there is one (the trigger route), so a rolled-back request leaves no claim.
        """
        token = uuid4()
        async with self.session() as db:
            row = await db.fetchone(
                f"""UPDATE tasks SET status = 'doing', completed_at = NULL,
                           claim_token = %(token)s, lease_expires_at = {_LEASE_UNTIL},
                           updated_at = now()
                    WHERE id = %(id)s AND agent_id = %(agent_id)s
                      AND (claim_token IS NULL OR lease_expires_at <= clock_timestamp())
                    RETURNING id""",
                {"id": task_id, "agent_id": agent_id, "token": token, "lease_seconds": lease_seconds},
            )
        return TaskClaim(task_id=task_id, token=token) if row is not None else None

    async def renew(self, claim: TaskClaim, *, lease_seconds: float) -> bool:
        """Extend the lease in its own committed transaction; False once the token no longer owns the row."""
        async with _committed() as db:
            row = await db.fetchone(
                f"UPDATE tasks SET lease_expires_at = {_LEASE_UNTIL} "
                "WHERE id = %(id)s AND claim_token = %(token)s "
                "AND lease_expires_at > clock_timestamp() RETURNING id",
                {"id": claim.task_id, "token": claim.token, "lease_seconds": lease_seconds},
            )
        return row is not None

    async def complete(self, claim: TaskClaim, *, status: TaskStatus, log: str) -> bool:
        """Finish a run as ``done`` (stamping ``completed_at``) or back to ``pending``, and log it.

        A stale token changes nothing, not even the log.
        """
        match status:
            case TaskStatus.DONE:
                sql = _SETTLE_DONE
            case TaskStatus.PENDING:
                sql = _SETTLE_PENDING
            case _:
                raise ValueError(f"A run cannot be completed as {status.value}")
        return await self._settle(claim, sql, log)

    async def release(self, claim: TaskClaim, *, log: str) -> bool:
        """Give the task back as ``pending`` with the reason logged; a stale token changes nothing."""
        return await self._settle(claim, _SETTLE_PENDING, log)

    async def _settle(self, claim: TaskClaim, sql: str, log: str) -> bool:
        async with _committed() as db:
            row = await db.fetchone(sql, {"id": claim.task_id, "token": claim.token, "content": log})
        return row is not None

    async def recover_expired(self, *, limit: int, log: str) -> list[UUID]:
        """Return runs whose lease expired to ``pending`` with a log entry; never reruns them.

        Rows another worker is settling right now are skipped rather than waited on.
        """
        async with _committed() as db:
            rows = await db.fetchall(
                """WITH expired AS (
                       SELECT id FROM tasks
                       WHERE claim_token IS NOT NULL AND lease_expires_at <= clock_timestamp()
                       ORDER BY lease_expires_at, id LIMIT %(limit)s FOR UPDATE SKIP LOCKED
                   ), recovered AS (
                        UPDATE tasks AS t SET status = 'pending', completed_at = NULL,
                               claim_token = NULL, lease_expires_at = NULL,
                              updated_at = now()
                       FROM expired
                       WHERE t.id = expired.id AND t.claim_token IS NOT NULL
                         AND t.lease_expires_at <= clock_timestamp()
                       RETURNING t.id
                   ) INSERT INTO task_logs (id, task_id, content)
                     SELECT gen_random_uuid(), id, %(content)s FROM recovered RETURNING task_id""",
                {"limit": limit, "content": log},
            )
        return [uuid_from_row(row["task_id"]) for row in rows]

    async def delete_with_logs(self, task_id: UUID) -> TaskRecord | None:
        """Delete task logs then the task row; return the deleted task when present."""
        async with self.session() as db:
            await db.execute(
                "DELETE FROM task_logs WHERE task_id = %(task_id)s",
                {"task_id": task_id},
            )
            row = await db.fetchone(
                f"DELETE FROM tasks WHERE id = %(id)s RETURNING {self._select_list()}",
                {"id": task_id},
            )
            return TaskRecord.from_row(row) if row is not None else None


@final
class TaskLogDAO(BaseDAO[TaskLogRecord]):
    table: ClassVar[str] = "task_logs"
    columns: ClassVar[tuple[str, ...]] = _LOG_COLUMNS
    record_factory = staticmethod(TaskLogRecord.from_row)

    async def list_for_task(self, task_id: UUID) -> Sequence[TaskLogRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM task_logs WHERE task_id = %(task_id)s ORDER BY created_at ASC",
                {"task_id": task_id},
            )
            return [TaskLogRecord.from_row(row) for row in rows]

    async def latest_for_task(self, task_id: UUID) -> TaskLogRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM task_logs "
                "WHERE task_id = %(task_id)s ORDER BY created_at DESC LIMIT 1",
                {"task_id": task_id},
            )
            return TaskLogRecord.from_row(row) if row is not None else None


task_dao = TaskDAO()
task_log_dao = TaskLogDAO()
