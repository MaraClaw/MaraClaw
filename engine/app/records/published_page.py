"""Published page records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.core.json_types import datetime_from_row, int_from_row, str_from_row, uuid_from_row, uuid_from_row_opt


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
    def from_row(cls, row: Mapping[str, object]) -> PublishedPageRecord:
        return cls(
            id=uuid_from_row(row["id"]),
            short_id=str_from_row(row["short_id"]),
            agent_id=uuid_from_row(row["agent_id"]),
            user_id=uuid_from_row(row["user_id"]),
            source_path=str_from_row(row["source_path"]),
            title=str_from_row(row.get("title")),
            view_count=int_from_row(row.get("view_count")),
            tenant_id=uuid_from_row_opt(row.get("tenant_id")),
            created_at=datetime_from_row(row.get("created_at")),
        )
