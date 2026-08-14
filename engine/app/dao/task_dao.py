"""DAO for tasks and task_logs (psycopg)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar, final
from uuid import UUID

from app.core.json_types import int_from_row, str_from_row
from app.dao.base import BaseDAO
from app.records.task import TaskLogRecord, TaskRecord

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
)

_LOG_COLUMNS = ("id", "task_id", "content", "created_at")


@final
class TaskDAO(BaseDAO[TaskRecord]):
    table: ClassVar[str] = "tasks"
    columns: ClassVar[tuple[str, ...]] = _TASK_COLUMNS
    record_factory = staticmethod(TaskRecord.from_row)

    async def list_for_agent(
        self,
        agent_id: UUID,
        *,
        status: str | None = None,
        type: str | None = None,
        ascending: bool = False,
    ) -> Sequence[TaskRecord]:
        params: dict[str, Any] = {"agent_id": agent_id}
        clauses = ["agent_id = %(agent_id)s"]
        if status:
            clauses.append("status = %(status)s")
            params["status"] = status
        if type:
            clauses.append("type = %(type)s")
            params["type"] = type
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
            return TaskRecord.from_row(row) if row else None

    async def count_for_agent(self, agent_id: UUID, *, status: str | None = None) -> int:
        params: dict[str, Any] = {"agent_id": agent_id}
        status_sql = ""
        if status:
            status_sql = " AND status = %(status)s"
            params["status"] = status
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
                + "FROM tasks t JOIN agents a ON a.id = t.agent_id "
                + "WHERE t.type = 'supervision' AND t.status = ANY(%(statuses)s) "
                + "AND t.remind_schedule IS NOT NULL",
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
                + "WHERE agent_id = %(agent_id)s AND title ILIKE %(pattern)s "
                + "ORDER BY created_at DESC LIMIT 1",
                {"agent_id": agent_id, "pattern": f"%{title}%"},
            )
            return TaskRecord.from_row(row) if row else None

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
            return TaskRecord.from_row(row) if row else None


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
                + "WHERE task_id = %(task_id)s ORDER BY created_at DESC LIMIT 1",
                {"task_id": task_id},
            )
            return TaskLogRecord.from_row(row) if row else None


task_dao = TaskDAO()
task_log_dao = TaskLogDAO()
