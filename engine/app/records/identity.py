"""Identity and identity-provider records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from app.core.json_types import (
    datetime_from_row,
    mapping_from_row,
    str_from_row,
    uuid_from_row,
    uuid_from_row_opt,
)


class AuthProviderType(StrEnum):
    """Supported authentication provider types."""

    FEISHU = "feishu"
    DINGTALK = "dingtalk"
    WECOM = "wecom"
    GOOGLE_WORKSPACE = "google_workspace"
    MICROSOFT_TEAMS = "microsoft_teams"
    GOOGLE = "google"
    GITHUB = "github"


@dataclass(slots=True)
class IdentityRecord:
    """Global physical identity (login credentials)."""

    id: UUID
    email: str | None = None
    phone: str | None = None
    username: str | None = None
    password_hash: str | None = None
    is_active: bool = True
    is_platform_admin: bool = False
    email_verified: bool = False
    must_change_password: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> IdentityRecord:
        return cls(
            id=uuid_from_row(row["id"]),
            email=str_from_row(row["email"]) or None,
            phone=str_from_row(row["phone"]) or None,
            username=str_from_row(row["username"]) or None,
            password_hash=str_from_row(row["password_hash"]) or None,
            is_active=bool(row.get("is_active", True)),
            is_platform_admin=bool(row.get("is_platform_admin", False)),
            email_verified=bool(row.get("email_verified", False)),
            must_change_password=bool(row.get("must_change_password", False)),
            created_at=datetime_from_row(row.get("created_at")),
            updated_at=datetime_from_row(row.get("updated_at")),
        )


@dataclass(slots=True)
class IdentityProviderRecord:
    """External identity provider configuration."""

    id: UUID
    provider_type: str
    name: str
    is_active: bool = True
    sso_login_enabled: bool = False
    config: dict[str, Any] = field(default_factory=dict[str, Any])
    tenant_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> IdentityProviderRecord:
        config = mapping_from_row(row.get("config") or {})
        return cls(
            id=uuid_from_row(row["id"]),
            provider_type=str_from_row(row["provider_type"]),
            name=str_from_row(row["name"]),
            is_active=bool(row.get("is_active", True)),
            sso_login_enabled=bool(row.get("sso_login_enabled", False)),
            config=config,
            tenant_id=uuid_from_row_opt(row.get("tenant_id")),
            created_at=datetime_from_row(row.get("created_at")),
            updated_at=datetime_from_row(row.get("updated_at")),
        )
