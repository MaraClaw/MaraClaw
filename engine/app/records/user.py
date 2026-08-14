"""Tenant-scoped user records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from app.records.identity import IdentityRecord


@dataclass(slots=True)
class UserRecord:
    """Tenant member profile linked to a global identity."""

    id: UUID
    identity_id: UUID | None = None
    tenant_id: UUID | None = None
    display_name: str = ""
    avatar_url: str | None = None
    title: str | None = None
    role: str = "member"
    is_active: bool = True
    registration_source: str | None = "web"
    created_at: datetime | None = None
    updated_at: datetime | None = None
    quota_message_limit: int = 50
    quota_message_period: str = "permanent"
    quota_messages_used: int = 0
    quota_period_start: datetime | None = None
    quota_max_agents: int = 2
    quota_agent_ttl_hours: int = 0
    is_genesis: bool = False
    identity: IdentityRecord | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any], *, identity: IdentityRecord | None = None) -> UserRecord:
        return cls(
            id=row["id"],
            identity_id=row.get("identity_id"),
            tenant_id=row.get("tenant_id"),
            display_name=row.get("display_name") or "",
            avatar_url=row.get("avatar_url"),
            title=row.get("title"),
            role=row.get("role") or "member",
            is_active=bool(row.get("is_active", True)),
            registration_source=row.get("registration_source"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
            quota_message_limit=int(row.get("quota_message_limit") or 50),
            quota_message_period=row.get("quota_message_period") or "permanent",
            quota_messages_used=int(row.get("quota_messages_used") or 0),
            quota_period_start=row.get("quota_period_start"),
            quota_max_agents=int(row.get("quota_max_agents") or 2),
            quota_agent_ttl_hours=int(row.get("quota_agent_ttl_hours") or 0),
            is_genesis=bool(row.get("is_genesis", False)),
            identity=identity,
        )

    # Association-proxy compatible accessors used across auth/registration.

    @property
    def email(self) -> str | None:
        return self.identity.email if self.identity else None

    @email.setter
    def email(self, value: str | None) -> None:
        if self.identity is not None:
            self.identity.email = value

    @property
    def username(self) -> str | None:
        return self.identity.username if self.identity else None

    @username.setter
    def username(self, value: str | None) -> None:
        if self.identity is not None:
            self.identity.username = value

    @property
    def password_hash(self) -> str | None:
        return self.identity.password_hash if self.identity else None

    @password_hash.setter
    def password_hash(self, value: str | None) -> None:
        if self.identity is not None:
            self.identity.password_hash = value

    @property
    def email_verified(self) -> bool:
        return bool(self.identity.email_verified) if self.identity else False

    @email_verified.setter
    def email_verified(self, value: bool) -> None:
        if self.identity is not None:
            self.identity.email_verified = bool(value)

    @property
    def is_platform_admin(self) -> bool:
        if self.role == "platform_admin":
            return True
        return bool(self.identity and self.identity.is_platform_admin)

    @property
    def must_change_password(self) -> bool:
        return bool(self.identity and self.identity.must_change_password)

    @property
    def primary_mobile(self) -> str | None:
        return self.identity.phone if self.identity else None

    @primary_mobile.setter
    def primary_mobile(self, value: str | None) -> None:
        if self.identity is not None:
            self.identity.phone = value
