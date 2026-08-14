"""Agent focus item records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from app.core.json_types import mapping_from_row


@dataclass(slots=True)
class AgentFocusItemRecord:
    """Structured focus item tracked by an agent."""

    id: UUID
    agent_id: UUID
    key: str
    description: str = ""
    title: str | None = None
    status: str = "in_progress"
    kind: str = "normal"
    source: str = "user"
    item_metadata: dict[str, Any] = field(default_factory=dict[str, Any])
    sort_order: int = 0
    completed_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> AgentFocusItemRecord:
        meta = mapping_from_row(row.get("metadata") or row.get("item_metadata") or {})
        return cls(
            id=row["id"],
            agent_id=row["agent_id"],
            key=row["key"],
            description=row.get("description") or "",
            title=row.get("title"),
            status=row.get("status") or "in_progress",
            kind=row.get("kind") or "normal",
            source=row.get("source") or "user",
            item_metadata=meta,
            sort_order=int(row.get("sort_order") or 0),
            completed_at=row.get("completed_at"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )
