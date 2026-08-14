"""Organization member records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.core.json_types import datetime_from_row, str_from_row, uuid_from_row, uuid_from_row_opt


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
    def from_row(cls, row: Mapping[str, object]) -> OrgMemberRecord:
        return cls(
            id=uuid_from_row(row["id"]),
            name=str_from_row(row["name"]),
            open_id=str_from_row(row["open_id"]) or None,
            unionid=str_from_row(row["unionid"]) or None,
            external_id=str_from_row(row["external_id"]) or None,
            provider_id=uuid_from_row_opt(row.get("provider_id")),
            name_translit_full=str_from_row(row["name_translit_full"]) or None,
            name_translit_initial=str_from_row(row["name_translit_initial"]) or None,
            email=str_from_row(row["email"]) or None,
            avatar_url=str_from_row(row.get("avatar_url")) or None,
            title=str_from_row(row.get("title")),
            department_id=uuid_from_row_opt(row.get("department_id")),
            department_path=str_from_row(row.get("department_path")),
            phone=str_from_row(row["phone"]) or None,
            status=str_from_row(row.get("status"), "active") or "active",
            tenant_id=uuid_from_row_opt(row.get("tenant_id")),
            user_id=uuid_from_row_opt(row.get("user_id")),
            synced_at=datetime_from_row(row.get("synced_at")),
        )
