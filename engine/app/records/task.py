"""Task and task-log records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.core.json_types import datetime_from_row, str_from_row, uuid_from_row, uuid_from_row_opt


@dataclass(slots=True)
class TaskRecord:
    """Task assigned to or managed by a digital employee."""

    id: UUID
    agent_id: UUID
    title: str
    created_by: UUID
    description: str | None = None
    type: str = "todo"
    status: str = "pending"
    priority: str = "medium"
    assignee: str = "self"
    due_date: datetime | None = None
    supervision_target_user_id: UUID | None = None
    supervision_target_name: str | None = None
    supervision_channel: str | None = None
    remind_schedule: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> TaskRecord:
        return cls(
            id=uuid_from_row(row["id"]),
            agent_id=uuid_from_row(row["agent_id"]),
            title=str_from_row(row["title"]),
            created_by=uuid_from_row(row["created_by"]),
            description=str_from_row(row["description"]) or None,
            type=str_from_row(row.get("type"), "todo") or "todo",
            status=str_from_row(row.get("status"), "pending") or "pending",
            priority=str_from_row(row.get("priority"), "medium") or "medium",
            assignee=str_from_row(row.get("assignee"), "self") or "self",
            due_date=datetime_from_row(row.get("due_date")),
            supervision_target_user_id=uuid_from_row_opt(row.get("supervision_target_user_id")),
            supervision_target_name=str_from_row(row["supervision_target_name"]) or None,
            supervision_channel=str_from_row(row["supervision_channel"]) or None,
            remind_schedule=str_from_row(row["remind_schedule"]) or None,
            created_at=datetime_from_row(row.get("created_at")),
            updated_at=datetime_from_row(row.get("updated_at")),
            completed_at=datetime_from_row(row.get("completed_at")),
        )


@dataclass(slots=True)
class TaskLogRecord:
    """Progress log entry for a task."""

    id: UUID
    task_id: UUID
    content: str
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> TaskLogRecord:
        return cls(
            id=uuid_from_row(row["id"]),
            task_id=uuid_from_row(row["task_id"]),
            content=str_from_row(row.get("content")),
            created_at=datetime_from_row(row.get("created_at")),
        )
