"""Enterprise info records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from app.core.json_types import (
    datetime_from_row,
    int_from_row,
    mapping_from_row,
    str_from_row,
    str_list_from_row,
    uuid_from_row,
    uuid_from_row_opt,
)


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
    def from_row(cls, row: Mapping[str, object]) -> EnterpriseInfoRecord:
        content = mapping_from_row(row.get("content") or {})
        return cls(
            id=uuid_from_row(row["id"]),
            info_type=str_from_row(row["info_type"]),
            content=content,
            version=int_from_row(row.get("version"), 1),
            visible_roles=str_list_from_row(row.get("visible_roles") or []),
            updated_by=uuid_from_row_opt(row.get("updated_by")),
            created_at=datetime_from_row(row.get("created_at")),
            updated_at=datetime_from_row(row.get("updated_at")),
        )
