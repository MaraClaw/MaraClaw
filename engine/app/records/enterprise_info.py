"""Enterprise info records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(slots=True)
class EnterpriseInfoRecord:
    """Versioned enterprise information blob for agent sync."""

    id: UUID
    info_type: str
    content: dict[str, Any] = field(default_factory=dict)
    version: int = 1
    visible_roles: list[str] = field(default_factory=list)
    updated_by: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> EnterpriseInfoRecord:
        content = row.get("content") or {}
        if not isinstance(content, dict):
            content = dict(content)
        roles = row.get("visible_roles") or []
        if not isinstance(roles, list):
            roles = list(roles)
        return cls(
            id=row["id"],
            info_type=row["info_type"],
            content=content,
            version=int(row.get("version") or 1),
            visible_roles=[str(r) for r in roles],
            updated_by=row.get("updated_by"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )
