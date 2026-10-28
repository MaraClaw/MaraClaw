"""Org department records for the tenant directory."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.core.json_types import datetime_from_row, str_from_row, uuid_from_row


@dataclass(frozen=True, slots=True)
class OrgDepartmentRecord:
    id: UUID
    tenant_id: UUID
    name: str
    created_at: datetime
    description: str = ""

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> OrgDepartmentRecord:
        created_at = datetime_from_row(row["created_at"])
        if created_at is None:
            raise ValueError("Department created_at is required")
        return cls(
            uuid_from_row(row["id"]),
            uuid_from_row(row["tenant_id"]),
            str_from_row(row["name"]),
            created_at,
            str_from_row(row["description"]),
        )
