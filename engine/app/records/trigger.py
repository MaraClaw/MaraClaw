"""Trigger and trigger-execution records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from app.core.json_types import (
    datetime_from_row,
    int_from_row,
    mapping_from_row,
    str_from_row,
    uuid_from_row,
)


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
    def from_row(cls, row: Mapping[str, object]) -> AgentTriggerRecord:
        config = mapping_from_row(row.get("config") or {})
        return cls(
            id=uuid_from_row(row["id"]),
            agent_id=uuid_from_row(row["agent_id"]),
            name=str_from_row(row.get("name")),
            type=str_from_row(row.get("type"), "webhook") or "webhook",
            config=config,
            reason=str_from_row(row.get("reason")),
            focus_ref=str_from_row(row["focus_ref"]) or None,
            is_enabled=bool(row.get("is_enabled", True)),
            last_fired_at=datetime_from_row(row.get("last_fired_at")),
            fire_count=int_from_row(row.get("fire_count")),
            max_fires=int_from_row(row["max_fires"]) if row.get("max_fires") is not None else None,
            cooldown_seconds=int_from_row(row.get("cooldown_seconds"), 60),
            is_system=bool(row.get("is_system", False)),
            created_at=datetime_from_row(row.get("created_at")),
            expires_at=datetime_from_row(row.get("expires_at")),
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
    def from_row(cls, row: Mapping[str, object]) -> TriggerExecutionRecord:
        payload = mapping_from_row(row.get("payload") or {})
        return cls(
            id=uuid_from_row(row["id"]),
            trigger_id=uuid_from_row(row["trigger_id"]),
            agent_id=uuid_from_row(row["agent_id"]),
            source=str_from_row(row.get("source"), "webhook") or "webhook",
            status=str_from_row(row.get("status"), "pending") or "pending",
            idempotency_key=str_from_row(row.get("idempotency_key")),
            payload=payload,
            payload_text=str_from_row(row.get("payload_text")),
            lease_owner=str_from_row(row["lease_owner"]) or None,
            lease_expires_at=datetime_from_row(row.get("lease_expires_at")),
            scheduled_at=datetime_from_row(row.get("scheduled_at")),
            started_at=datetime_from_row(row.get("started_at")),
            finished_at=datetime_from_row(row.get("finished_at")),
            last_error=str_from_row(row["last_error"]) or None,
            created_at=datetime_from_row(row.get("created_at")),
        )
