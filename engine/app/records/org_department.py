"""Organization department records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(slots=True)
class OrgDepartmentRecord:
    """Department from an identity provider org structure."""

    id: UUID
    name: str
    external_id: str | None = None
    provider_id: UUID | None = None
    parent_id: UUID | None = None
    path: str = ""
    member_count: int = 0
    status: str = "active"
    tenant_id: UUID | None = None
    synced_at: datetime | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> OrgDepartmentRecord:
        return cls(
            id=row["id"],
            name=row["name"],
            external_id=row.get("external_id"),
            provider_id=row.get("provider_id"),
            parent_id=row.get("parent_id"),
            path=row.get("path") or "",
            member_count=int(row.get("member_count") or 0),
            status=row.get("status") or "active",
            tenant_id=row.get("tenant_id"),
            synced_at=row.get("synced_at"),
        )
