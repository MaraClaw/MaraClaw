"""Organization member records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from app.core.json_types import str_from_row, uuid_from_row, uuid_from_row_opt


@dataclass(slots=True)
class OrgMemberRecord:
    """Shared tenant identity linking a platform user to an external provider."""

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
    phone: str | None = None
    status: str = "active"
    tenant_id: UUID | None = None
    user_id: UUID | None = None

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
            phone=str_from_row(row["phone"]) or None,
            status=str_from_row(row.get("status"), "active") or "active",
            tenant_id=uuid_from_row_opt(row.get("tenant_id")),
            user_id=uuid_from_row_opt(row.get("user_id")),
        )
