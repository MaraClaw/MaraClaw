"""Workspace collaboration records (revisions / locks)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.core.json_types import datetime_from_row, int_from_row, str_from_row, uuid_from_row, uuid_from_row_opt


@dataclass(slots=True)
class WorkspaceFileRevisionRecord:
    """A single workspace file revision history row."""

    id: UUID
    agent_id: UUID
    path: str
    operation: str = "write"
    actor_type: str = "user"
    actor_id: UUID | None = None
    session_id: str | None = None
    before_content: str | None = None
    after_content: str | None = None
    content_hash: str = ""
    group_key: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> WorkspaceFileRevisionRecord:
        return cls(
            id=uuid_from_row(row["id"]),
            agent_id=uuid_from_row(row["agent_id"]),
            path=str_from_row(row["path"]),
            operation=str_from_row(row.get("operation"), "write") or "write",
            actor_type=str_from_row(row.get("actor_type"), "user") or "user",
            actor_id=uuid_from_row_opt(row.get("actor_id")),
            session_id=str_from_row(row["session_id"]) or None,
            before_content=str_from_row(row["before_content"]) or None,
            after_content=str_from_row(row["after_content"]) or None,
            content_hash=str_from_row(row.get("content_hash")),
            group_key=str_from_row(row["group_key"]) or None,
            created_at=datetime_from_row(row.get("created_at")),
            updated_at=datetime_from_row(row.get("updated_at")),
        )


@dataclass(slots=True)
class WorkspaceEditLockRecord:
    """Short-lived human edit lock for a workspace path."""

    id: UUID
    agent_id: UUID
    path: str
    user_id: UUID
    expires_at: datetime
    session_id: str | None = None
    heartbeat_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> WorkspaceEditLockRecord:
        expires_at = datetime_from_row(row.get("expires_at"))
        if expires_at is None:
            raise TypeError("workspace edit lock requires expires_at")
        return cls(
            id=uuid_from_row(row["id"]),
            agent_id=uuid_from_row(row["agent_id"]),
            path=str_from_row(row["path"]),
            user_id=uuid_from_row(row["user_id"]),
            expires_at=expires_at,
            session_id=str_from_row(row["session_id"]) or None,
            heartbeat_count=int_from_row(row.get("heartbeat_count")),
            created_at=datetime_from_row(row.get("created_at")),
            updated_at=datetime_from_row(row.get("updated_at")),
        )
