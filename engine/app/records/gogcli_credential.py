"""Gogcli credential state records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(slots=True)
class GogcliCredentialStateRecord:
    """Encrypted gogcli keyring and data snapshot for one agent."""

    id: UUID
    agent_id: UUID
    status: str = "unauthenticated"
    encrypted_keyring_password: str | None = None
    encrypted_gog_data_archive: str | None = None
    account_hint: str | None = None
    keyring_password_updated_at: datetime | None = None
    credential_snapshot_updated_at: datetime | None = None
    last_authenticated_at: datetime | None = None
    last_status_checked_at: datetime | None = None
    last_restored_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> GogcliCredentialStateRecord:
        return cls(
            id=row["id"],
            agent_id=row["agent_id"],
            status=row.get("status") or "unauthenticated",
            encrypted_keyring_password=row.get("encrypted_keyring_password"),
            encrypted_gog_data_archive=row.get("encrypted_gog_data_archive"),
            account_hint=row.get("account_hint"),
            keyring_password_updated_at=row.get("keyring_password_updated_at"),
            credential_snapshot_updated_at=row.get("credential_snapshot_updated_at"),
            last_authenticated_at=row.get("last_authenticated_at"),
            last_status_checked_at=row.get("last_status_checked_at"),
            last_restored_at=row.get("last_restored_at"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )
