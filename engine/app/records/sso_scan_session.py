"""SSO scan session records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


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
    def from_row(cls, row: dict[str, Any]) -> SSOScanSessionRecord:
        return cls(
            id=row["id"],
            expires_at=row["expires_at"],
            status=row.get("status") or "pending",
            provider_type=row.get("provider_type"),
            error_msg=row.get("error_msg"),
            tenant_id=row.get("tenant_id"),
            user_id=row.get("user_id"),
            access_token=row.get("access_token"),
            created_at=row.get("created_at"),
        )
