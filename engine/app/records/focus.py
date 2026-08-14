"""Agent focus item records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from app.core.json_types import datetime_from_row, int_from_row, mapping_from_row, str_from_row, uuid_from_row


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
    def from_row(cls, row: Mapping[str, object]) -> AgentFocusItemRecord:
        meta = mapping_from_row(row.get("metadata") or row.get("item_metadata") or {})
        return cls(
            id=uuid_from_row(row["id"]),
            agent_id=uuid_from_row(row["agent_id"]),
            key=str_from_row(row["key"]),
            description=str_from_row(row.get("description")),
            title=str_from_row(row["title"]) or None,
            status=str_from_row(row.get("status"), "in_progress") or "in_progress",
            kind=str_from_row(row.get("kind"), "normal") or "normal",
            source=str_from_row(row.get("source"), "user") or "user",
            item_metadata=meta,
            sort_order=int_from_row(row.get("sort_order")),
            completed_at=datetime_from_row(row.get("completed_at")),
            created_at=datetime_from_row(row.get("created_at")),
            updated_at=datetime_from_row(row.get("updated_at")),
        )
