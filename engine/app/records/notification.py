"""Notification records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(slots=True)
class NotificationRecord:
    """In-app notification for a user or agent."""

    id: UUID
    type: str
    title: str
    user_id: UUID | None = None
    agent_id: UUID | None = None
    body: str = ""
    link: str | None = None
    ref_id: UUID | None = None
    sender_name: str | None = None
    is_read: bool = False
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> NotificationRecord:
        return cls(
            id=row["id"],
            type=row["type"],
            title=row["title"],
            user_id=row.get("user_id"),
            agent_id=row.get("agent_id"),
            body=row.get("body") or "",
            link=row.get("link"),
            ref_id=row.get("ref_id"),
            sender_name=row.get("sender_name"),
            is_read=bool(row.get("is_read", False)),
            created_at=row.get("created_at"),
        )
