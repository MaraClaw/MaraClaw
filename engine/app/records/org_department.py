"""Organization department records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.core.json_types import datetime_from_row, int_from_row, str_from_row, uuid_from_row, uuid_from_row_opt


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
    def from_row(cls, row: Mapping[str, object]) -> OrgDepartmentRecord:
        return cls(
            id=uuid_from_row(row["id"]),
            name=str_from_row(row["name"]),
            external_id=str_from_row(row["external_id"]) or None,
            provider_id=uuid_from_row_opt(row.get("provider_id")),
            parent_id=uuid_from_row_opt(row.get("parent_id")),
            path=str_from_row(row.get("path")),
            member_count=int_from_row(row.get("member_count")),
            status=str_from_row(row.get("status"), "active") or "active",
            tenant_id=uuid_from_row_opt(row.get("tenant_id")),
            synced_at=datetime_from_row(row.get("synced_at")),
        )
