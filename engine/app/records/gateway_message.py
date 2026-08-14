"""Gateway message records for OpenClaw edge communication."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.core.json_types import datetime_from_row, str_from_row, uuid_from_row, uuid_from_row_opt


@dataclass(slots=True)
class GatewayMessageRecord:
    """Message queued for delivery to an OpenClaw agent."""

    id: UUID
    agent_id: UUID
    content: str
    status: str = "pending"
    sender_agent_id: UUID | None = None
    sender_user_id: UUID | None = None
    conversation_id: str | None = None
    result: str | None = None
    created_at: datetime | None = None
    delivered_at: datetime | None = None
    completed_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> GatewayMessageRecord:
        return cls(
            id=uuid_from_row(row["id"]),
            agent_id=uuid_from_row(row["agent_id"]),
            content=str_from_row(row.get("content")),
            status=str_from_row(row.get("status"), "pending") or "pending",
            sender_agent_id=uuid_from_row_opt(row.get("sender_agent_id")),
            sender_user_id=uuid_from_row_opt(row.get("sender_user_id")),
            conversation_id=str_from_row(row["conversation_id"]) or None,
            result=str_from_row(row["result"]) or None,
            created_at=datetime_from_row(row.get("created_at")),
            delivered_at=datetime_from_row(row.get("delivered_at")),
            completed_at=datetime_from_row(row.get("completed_at")),
        )
