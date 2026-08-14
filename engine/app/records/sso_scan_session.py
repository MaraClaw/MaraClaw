"""SSO scan session records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.core.json_types import datetime_from_row, str_from_row, uuid_from_row, uuid_from_row_opt


@dataclass(slots=True)
class SSOScanSessionRecord:
    """Temporary session for SSO QR code scanning/login."""

    id: UUID
    expires_at: datetime
    status: str = "pending"
    provider_type: str | None = None
    error_msg: str | None = None
    tenant_id: UUID | None = None
    user_id: UUID | None = None
    access_token: str | None = None
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> SSOScanSessionRecord:
        expires_at = datetime_from_row(row.get("expires_at"))
        if expires_at is None:
            raise TypeError("sso scan session requires expires_at")
        return cls(
            id=uuid_from_row(row["id"]),
            expires_at=expires_at,
            status=str_from_row(row.get("status"), "pending") or "pending",
            provider_type=str_from_row(row["provider_type"]) or None,
            error_msg=str_from_row(row["error_msg"]) or None,
            tenant_id=uuid_from_row_opt(row.get("tenant_id")),
            user_id=uuid_from_row_opt(row.get("user_id")),
            access_token=str_from_row(row["access_token"]) or None,
            created_at=datetime_from_row(row.get("created_at")),
        )
