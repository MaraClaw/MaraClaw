"""Published page records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(slots=True)
class PublishedPageRecord:
    """Public HTML page published from an agent workspace."""

    id: UUID
    short_id: str
    agent_id: UUID
    user_id: UUID
    source_path: str
    title: str = ""
    view_count: int = 0
    tenant_id: UUID | None = None
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> PublishedPageRecord:
        return cls(
            id=row["id"],
            short_id=row["short_id"],
            agent_id=row["agent_id"],
            user_id=row["user_id"],
            source_path=row["source_path"],
            title=row.get("title") or "",
            view_count=int(row.get("view_count") or 0),
            tenant_id=row.get("tenant_id"),
            created_at=row.get("created_at"),
        )
