"""Identity and identity-provider records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from app.core.json_types import mapping_from_row


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
    def from_row(cls, row: dict[str, Any]) -> IdentityRecord:
        return cls(
            id=row["id"],
            email=row.get("email"),
            phone=row.get("phone"),
            username=row.get("username"),
            password_hash=row.get("password_hash"),
            is_active=bool(row.get("is_active", True)),
            is_platform_admin=bool(row.get("is_platform_admin", False)),
            email_verified=bool(row.get("email_verified", False)),
            must_change_password=bool(row.get("must_change_password", False)),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
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
    def from_row(cls, row: dict[str, Any]) -> IdentityProviderRecord:
        config = mapping_from_row(row.get("config") or {})
        return cls(
            id=row["id"],
            provider_type=str(row["provider_type"]),
            name=row["name"],
            is_active=bool(row.get("is_active", True)),
            sso_login_enabled=bool(row.get("sso_login_enabled", False)),
            config=config,
            tenant_id=row.get("tenant_id"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )
