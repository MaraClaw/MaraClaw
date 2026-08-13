"""Agent credential records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


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
    def from_row(cls, row: dict[str, Any]) -> AgentCredentialRecord:
        return cls(
            id=row["id"],
            agent_id=row["agent_id"],
            platform=row["platform"],
            credential_type=row.get("credential_type") or "website",
            display_name=row.get("display_name") or "",
            cookies_json=row.get("cookies_json"),
            cookies_updated_at=row.get("cookies_updated_at"),
            status=row.get("status") or "active",
            last_login_at=row.get("last_login_at"),
            last_injected_at=row.get("last_injected_at"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )
