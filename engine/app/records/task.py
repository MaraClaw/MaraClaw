"""Task and task-log records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


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
    def from_row(cls, row: dict[str, Any]) -> TaskRecord:
        return cls(
            id=row["id"],
            agent_id=row["agent_id"],
            title=row["title"],
            created_by=row["created_by"],
            description=row.get("description"),
            type=row.get("type") or "todo",
            status=row.get("status") or "pending",
            priority=row.get("priority") or "medium",
            assignee=row.get("assignee") or "self",
            due_date=row.get("due_date"),
            supervision_target_user_id=row.get("supervision_target_user_id"),
            supervision_target_name=row.get("supervision_target_name"),
            supervision_channel=row.get("supervision_channel"),
            remind_schedule=row.get("remind_schedule"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
            completed_at=row.get("completed_at"),
        )


@dataclass(slots=True)
class TaskLogRecord:
    """Progress log entry for a task."""

    id: UUID
    task_id: UUID
    content: str
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> TaskLogRecord:
        return cls(
            id=row["id"],
            task_id=row["task_id"],
            content=row.get("content") or "",
            created_at=row.get("created_at"),
        )
