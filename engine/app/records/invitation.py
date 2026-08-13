"""Invitation code records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


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
    def from_row(cls, row: dict[str, Any]) -> InvitationCodeRecord:
        return cls(
            id=row["id"],
            code=row["code"],
            tenant_id=row.get("tenant_id"),
            max_uses=int(row.get("max_uses") or 1),
            used_count=int(row.get("used_count") or 0),
            is_active=bool(row.get("is_active", True)),
            created_by=row.get("created_by"),
            created_at=row.get("created_at"),
        )
