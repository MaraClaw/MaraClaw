"""Gateway message records for OpenClaw edge communication."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


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
    def from_row(cls, row: dict[str, Any]) -> GatewayMessageRecord:
        return cls(
            id=row["id"],
            agent_id=row["agent_id"],
            content=row.get("content") or "",
            status=row.get("status") or "pending",
            sender_agent_id=row.get("sender_agent_id"),
            sender_user_id=row.get("sender_user_id"),
            conversation_id=row.get("conversation_id"),
            result=row.get("result"),
            created_at=row.get("created_at"),
            delivered_at=row.get("delivered_at"),
            completed_at=row.get("completed_at"),
        )
