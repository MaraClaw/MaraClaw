"""Email domains claimed by a tenant."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(slots=True)
class TenantEmailDomainRecord:
    """One claimed email domain for an organization."""

    id: UUID
    tenant_id: UUID
    domain: str
    is_default: bool = False
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> TenantEmailDomainRecord:
        return cls(
            id=row["id"],
            tenant_id=row["tenant_id"],
            domain=row["domain"],
            is_default=bool(row.get("is_default", False)),
            created_at=row.get("created_at"),
        )
