"""Email domains claimed by a tenant."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.core.json_types import datetime_from_row, str_from_row, uuid_from_row


@dataclass(slots=True)
class TenantEmailDomainRecord:
    """One claimed email domain for an organization."""

    id: UUID
    tenant_id: UUID
    domain: str
    is_default: bool = False
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> TenantEmailDomainRecord:
        return cls(
            id=uuid_from_row(row["id"]),
            tenant_id=uuid_from_row(row["tenant_id"]),
            domain=str_from_row(row["domain"]),
            is_default=bool(row.get("is_default", False)),
            created_at=datetime_from_row(row.get("created_at")),
        )
