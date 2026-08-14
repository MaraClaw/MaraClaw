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


@dataclass(slots=True)
class AdminAuditLogRecord:
    """Admin action trail: who did what, when, and which fields changed."""

    id: UUID
    actor_id: UUID | None
    actor_role: str
    actor_email: str | None
    action: str
    target_type: str
    target_id: UUID | None
    tenant_id: UUID | None
    changes: dict[str, Any] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)
    ip_address: str | None = None
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> AdminAuditLogRecord:
        changes = row.get("changes") or {}
        details = row.get("details") or {}
        if not isinstance(changes, dict):
            changes = dict(changes)
        if not isinstance(details, dict):
            details = dict(details)
        return cls(
            id=row["id"],
            actor_id=row.get("actor_id"),
            actor_role=row.get("actor_role") or "",
            actor_email=row.get("actor_email"),
            action=row["action"],
            target_type=row.get("target_type") or "",
            target_id=row.get("target_id"),
            tenant_id=row.get("tenant_id"),
            changes=changes,
            details=details,
            ip_address=row.get("ip_address"),
            created_at=row.get("created_at"),
        )
