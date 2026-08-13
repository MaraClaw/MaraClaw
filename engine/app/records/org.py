"""Organization member records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(slots=True)
class OrgMemberRecord:
    """Directory person from an identity provider org sync."""

    id: UUID
    name: str
    open_id: str | None = None
    unionid: str | None = None
    external_id: str | None = None
    provider_id: UUID | None = None
    name_translit_full: str | None = None
    name_translit_initial: str | None = None
    email: str | None = None
    avatar_url: str | None = None
    title: str = ""
    department_id: UUID | None = None
    department_path: str = ""
    phone: str | None = None
    status: str = "active"
    tenant_id: UUID | None = None
    user_id: UUID | None = None
    synced_at: datetime | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> OrgMemberRecord:
        return cls(
            id=row["id"],
            name=row["name"],
            open_id=row.get("open_id"),
            unionid=row.get("unionid"),
            external_id=row.get("external_id"),
            provider_id=row.get("provider_id"),
            name_translit_full=row.get("name_translit_full"),
            name_translit_initial=row.get("name_translit_initial"),
            email=row.get("email"),
            avatar_url=row.get("avatar_url"),
            title=row.get("title") or "",
            department_id=row.get("department_id"),
            department_path=row.get("department_path") or "",
            phone=row.get("phone"),
            status=row.get("status") or "active",
            tenant_id=row.get("tenant_id"),
            user_id=row.get("user_id"),
            synced_at=row.get("synced_at"),
        )
