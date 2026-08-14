"""Enterprise info records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from app.core.json_types import mapping_from_row, str_list_from_row


@dataclass(slots=True)
class EnterpriseInfoRecord:
    """Versioned enterprise information blob for agent sync."""

    id: UUID
    info_type: str
    content: dict[str, Any] = field(default_factory=dict[str, Any])
    version: int = 1
    visible_roles: list[str] = field(default_factory=list[str])
    updated_by: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> EnterpriseInfoRecord:
        content = mapping_from_row(row.get("content") or {})
        return cls(
            id=row["id"],
            info_type=row["info_type"],
            content=content,
            version=int(row.get("version") or 1),
            visible_roles=str_list_from_row(row.get("visible_roles") or []),
            updated_by=row.get("updated_by"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )
