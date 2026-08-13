"""Audit log and approval-request records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(slots=True)
class ApprovalRequestRecord:
    """L3 autonomy approval request row."""

    id: UUID
    agent_id: UUID
    action_type: str
    details: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"
    created_at: datetime | None = None
    resolved_at: datetime | None = None
    resolved_by: UUID | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> ApprovalRequestRecord:
        details = row.get("details") or {}
        if not isinstance(details, dict):
            details = dict(details)
        return cls(
            id=row["id"],
            agent_id=row["agent_id"],
            action_type=row["action_type"],
            details=details,
            status=row.get("status") or "pending",
            created_at=row.get("created_at"),
            resolved_at=row.get("resolved_at"),
            resolved_by=row.get("resolved_by"),
        )
