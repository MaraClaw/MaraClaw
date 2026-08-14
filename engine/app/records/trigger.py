"""Trigger and trigger-execution records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from app.core.json_types import mapping_from_row


@dataclass(slots=True)
class AgentTriggerRecord:
    """Agent-owned trigger definition."""

    id: UUID
    agent_id: UUID
    name: str = ""
    type: str = "webhook"
    config: dict[str, Any] = field(default_factory=dict[str, Any])
    reason: str = ""
    focus_ref: str | None = None
    is_enabled: bool = True
    last_fired_at: datetime | None = None
    fire_count: int = 0
    max_fires: int | None = None
    cooldown_seconds: int = 60
    is_system: bool = False
    created_at: datetime | None = None
    expires_at: datetime | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> AgentTriggerRecord:
        config = mapping_from_row(row.get("config") or {})
        return cls(
            id=row["id"],
            agent_id=row["agent_id"],
            name=row.get("name") or "",
            type=row.get("type") or "webhook",
            config=config,
            reason=row.get("reason") or "",
            focus_ref=row.get("focus_ref"),
            is_enabled=bool(row.get("is_enabled", True)),
            last_fired_at=row.get("last_fired_at"),
            fire_count=int(row.get("fire_count") or 0),
            max_fires=row.get("max_fires"),
            cooldown_seconds=int(row.get("cooldown_seconds") or 60),
            is_system=bool(row.get("is_system", False)),
            created_at=row.get("created_at"),
            expires_at=row.get("expires_at"),
        )


@dataclass(slots=True)
class TriggerExecutionRecord:
    """Durable trigger execution queue row."""

    id: UUID
    trigger_id: UUID
    agent_id: UUID
    source: str = "webhook"
    status: str = "pending"
    idempotency_key: str = ""
    payload: dict[str, Any] = field(default_factory=dict[str, Any])
    payload_text: str = ""
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    scheduled_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> TriggerExecutionRecord:
        payload = mapping_from_row(row.get("payload") or {})
        return cls(
            id=row["id"],
            trigger_id=row["trigger_id"],
            agent_id=row["agent_id"],
            source=row.get("source") or "webhook",
            status=row.get("status") or "pending",
            idempotency_key=row.get("idempotency_key") or "",
            payload=payload,
            payload_text=row.get("payload_text") or "",
            lease_owner=row.get("lease_owner"),
            lease_expires_at=row.get("lease_expires_at"),
            scheduled_at=row.get("scheduled_at"),
            started_at=row.get("started_at"),
            finished_at=row.get("finished_at"),
            last_error=row.get("last_error"),
            created_at=row.get("created_at"),
        )
