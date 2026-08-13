"""Workspace collaboration records (revisions / locks)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


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
    def from_row(cls, row: dict[str, Any]) -> WorkspaceFileRevisionRecord:
        return cls(
            id=row["id"],
            agent_id=row["agent_id"],
            path=row["path"],
            operation=row.get("operation") or "write",
            actor_type=row.get("actor_type") or "user",
            actor_id=row.get("actor_id"),
            session_id=row.get("session_id"),
            before_content=row.get("before_content"),
            after_content=row.get("after_content"),
            content_hash=row.get("content_hash") or "",
            group_key=row.get("group_key"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
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
    def from_row(cls, row: dict[str, Any]) -> WorkspaceEditLockRecord:
        return cls(
            id=row["id"],
            agent_id=row["agent_id"],
            path=row["path"],
            user_id=row["user_id"],
            expires_at=row["expires_at"],
            session_id=row.get("session_id"),
            heartbeat_count=int(row.get("heartbeat_count") or 0),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )
