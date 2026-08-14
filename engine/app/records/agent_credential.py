"""Agent credential records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.core.json_types import datetime_from_row, str_from_row, uuid_from_row


@dataclass(slots=True)
class AgentCredentialRecord:
    """Encrypted session cookies for an agent on a platform."""

    id: UUID
    agent_id: UUID
    platform: str
    credential_type: str = "website"
    display_name: str = ""
    cookies_json: str | None = None
    cookies_updated_at: datetime | None = None
    status: str = "active"
    last_login_at: datetime | None = None
    last_injected_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> AgentCredentialRecord:
        return cls(
            id=uuid_from_row(row["id"]),
            agent_id=uuid_from_row(row["agent_id"]),
            platform=str_from_row(row["platform"]),
            credential_type=str_from_row(row.get("credential_type"), "website") or "website",
            display_name=str_from_row(row.get("display_name")),
            cookies_json=str_from_row(row["cookies_json"]) or None,
            cookies_updated_at=datetime_from_row(row.get("cookies_updated_at")),
            status=str_from_row(row.get("status"), "active") or "active",
            last_login_at=datetime_from_row(row.get("last_login_at")),
            last_injected_at=datetime_from_row(row.get("last_injected_at")),
            created_at=datetime_from_row(row.get("created_at")),
            updated_at=datetime_from_row(row.get("updated_at")),
        )
