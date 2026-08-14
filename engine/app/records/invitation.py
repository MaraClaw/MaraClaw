"""Invitation code records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.core.json_types import datetime_from_row, int_from_row, str_from_row, uuid_from_row, uuid_from_row_opt


@dataclass(slots=True)
class InvitationCodeRecord:
    """Registration invitation code."""

    id: UUID
    code: str
    tenant_id: UUID | None = None
    max_uses: int = 1
    used_count: int = 0
    is_active: bool = True
    created_by: UUID | None = None
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> InvitationCodeRecord:
        return cls(
            id=uuid_from_row(row["id"]),
            code=str_from_row(row["code"]),
            tenant_id=uuid_from_row_opt(row.get("tenant_id")),
            max_uses=int_from_row(row.get("max_uses"), 1),
            used_count=int_from_row(row.get("used_count")),
            is_active=bool(row.get("is_active", True)),
            created_by=uuid_from_row_opt(row.get("created_by")),
            created_at=datetime_from_row(row.get("created_at")),
        )
