"""Channel configuration records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from app.core.json_types import datetime_from_row, mapping_from_row, str_from_row, uuid_from_row


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
    extra_config: dict[str, Any] = field(default_factory=dict[str, Any])
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> ChannelConfigRecord:
        extra = mapping_from_row(row.get("extra_config") or {})
        return cls(
            id=uuid_from_row(row["id"]),
            agent_id=uuid_from_row(row["agent_id"]),
            channel_type=str_from_row(row.get("channel_type"), "feishu") or "feishu",
            app_id=str_from_row(row["app_id"]) or None,
            app_secret=str_from_row(row["app_secret"]) or None,
            encrypt_key=str_from_row(row["encrypt_key"]) or None,
            verification_token=str_from_row(row["verification_token"]) or None,
            is_configured=bool(row.get("is_configured", False)),
            is_connected=bool(row.get("is_connected", False)),
            last_tested_at=datetime_from_row(row.get("last_tested_at")),
            extra_config=extra,
            created_at=datetime_from_row(row.get("created_at")),
            updated_at=datetime_from_row(row.get("updated_at")),
        )
