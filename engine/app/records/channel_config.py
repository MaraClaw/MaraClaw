"""Channel configuration records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(slots=True)
class ChannelConfigRecord:
    """Per-agent channel connector configuration."""

    id: UUID
    agent_id: UUID
    channel_type: str = "feishu"
    app_id: str | None = None
    app_secret: str | None = None
    encrypt_key: str | None = None
    verification_token: str | None = None
    is_configured: bool = False
    is_connected: bool = False
    last_tested_at: datetime | None = None
    extra_config: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> ChannelConfigRecord:
        extra = row.get("extra_config") or {}
        if not isinstance(extra, dict):
            extra = dict(extra)
        return cls(
            id=row["id"],
            agent_id=row["agent_id"],
            channel_type=row.get("channel_type") or "feishu",
            app_id=row.get("app_id"),
            app_secret=row.get("app_secret"),
            encrypt_key=row.get("encrypt_key"),
            verification_token=row.get("verification_token"),
            is_configured=bool(row.get("is_configured", False)),
            is_connected=bool(row.get("is_connected", False)),
            last_tested_at=row.get("last_tested_at"),
            extra_config=extra,
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )
