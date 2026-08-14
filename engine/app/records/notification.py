"""Notification records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.core.json_types import datetime_from_row, str_from_row, uuid_from_row, uuid_from_row_opt


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
    def from_row(cls, row: Mapping[str, object]) -> NotificationRecord:
        return cls(
            id=uuid_from_row(row["id"]),
            type=str_from_row(row["type"]),
            title=str_from_row(row["title"]),
            user_id=uuid_from_row_opt(row.get("user_id")),
            agent_id=uuid_from_row_opt(row.get("agent_id")),
            body=str_from_row(row.get("body")),
            link=str_from_row(row["link"]) or None,
            ref_id=uuid_from_row_opt(row.get("ref_id")),
            sender_name=str_from_row(row["sender_name"]) or None,
            is_read=bool(row.get("is_read", False)),
            created_at=datetime_from_row(row.get("created_at")),
        )
