"""Gogcli credential state records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.core.json_types import datetime_from_row, str_from_row, uuid_from_row


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
    def from_row(cls, row: Mapping[str, object]) -> GogcliCredentialStateRecord:
        return cls(
            id=uuid_from_row(row["id"]),
            agent_id=uuid_from_row(row["agent_id"]),
            status=str_from_row(row.get("status"), "unauthenticated") or "unauthenticated",
            encrypted_keyring_password=str_from_row(row["encrypted_keyring_password"]) or None,
            encrypted_gog_data_archive=str_from_row(row["encrypted_gog_data_archive"]) or None,
            account_hint=str_from_row(row["account_hint"]) or None,
            keyring_password_updated_at=datetime_from_row(row.get("keyring_password_updated_at")),
            credential_snapshot_updated_at=datetime_from_row(row.get("credential_snapshot_updated_at")),
            last_authenticated_at=datetime_from_row(row.get("last_authenticated_at")),
            last_status_checked_at=datetime_from_row(row.get("last_status_checked_at")),
            last_restored_at=datetime_from_row(row.get("last_restored_at")),
            created_at=datetime_from_row(row.get("created_at")),
            updated_at=datetime_from_row(row.get("updated_at")),
        )
