"""Audit log and approval-request records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from app.core.domain.approvals import ApprovalStatus
from app.core.json_types import (
    datetime_from_row,
    is_str_dict,
    mapping_from_row,
    str_from_row,
    uuid_from_row,
    uuid_from_row_opt,
)


@dataclass(slots=True)
class ApprovalRequestRecord:
    """L3 autonomy approval request row.

    ``details`` is the bounded, shareable summary. ``execution_args`` is the complete tool-argument object kept for
    replay: it is private (never serialized or logged) and only the claiming ``resolve`` loads it; ``None`` marks rows
    created before the column existed and rows loaded by list/get queries.
    """

    id: UUID
    agent_id: UUID
    action_type: str
    details: dict[str, Any] = field(default_factory=dict[str, Any])
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: datetime | None = None
    resolved_at: datetime | None = None
    resolved_by: UUID | None = None
    execution_args: dict[str, Any] | None = field(default=None, repr=False)

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> ApprovalRequestRecord:
        details = mapping_from_row(row.get("details") or {})
        execution_args = row.get("execution_args")
        return cls(
            id=uuid_from_row(row["id"]),
            agent_id=uuid_from_row(row["agent_id"]),
            action_type=str_from_row(row["action_type"]),
            details=details,
            status=ApprovalStatus(str_from_row(row.get("status"), "pending")),
            created_at=datetime_from_row(row.get("created_at")),
            resolved_at=datetime_from_row(row.get("resolved_at")),
            resolved_by=uuid_from_row_opt(row.get("resolved_by")),
            execution_args=execution_args if is_str_dict(execution_args) else None,
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
    changes: dict[str, Any] = field(default_factory=dict[str, Any])
    details: dict[str, Any] = field(default_factory=dict[str, Any])
    ip_address: str | None = None
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> AdminAuditLogRecord:
        changes = mapping_from_row(row.get("changes") or {})
        details = mapping_from_row(row.get("details") or {})
        return cls(
            id=uuid_from_row(row["id"]),
            actor_id=uuid_from_row_opt(row.get("actor_id")),
            actor_role=str_from_row(row.get("actor_role")),
            actor_email=str_from_row(row["actor_email"]) or None,
            action=str_from_row(row["action"]),
            target_type=str_from_row(row.get("target_type")),
            target_id=uuid_from_row_opt(row.get("target_id")),
            tenant_id=uuid_from_row_opt(row.get("tenant_id")),
            changes=changes,
            details=details,
            ip_address=str_from_row(row["ip_address"]) or None,
            created_at=datetime_from_row(row.get("created_at")),
        )
