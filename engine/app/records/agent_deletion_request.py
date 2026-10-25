"""Approval-gated agent deletion requests."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.core.json_types import datetime_from_row, str_from_row, uuid_from_row


class DeletionStatus(StrEnum):
    PENDING = "pending"
    EXECUTED = "executed"
    CANCELLED = "cancelled"


class CancelReason(StrEnum):
    WITHDRAWN = "withdrawn"
    OWNER_CHANGED = "owner_changed"
    TENANT_CHANGED = "tenant_changed"
    AGENT_GONE = "agent_gone"


@dataclass(slots=True)
class AgentDeletionRequestRecord:
    id: UUID
    tenant_id: UUID
    requester_id: UUID
    agent_id: UUID
    agent_name: str
    agent_creator_id: UUID
    reason: str
    created_at: datetime
    status: DeletionStatus = DeletionStatus.PENDING
    cancel_reason: CancelReason | None = None
    resolved_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> AgentDeletionRequestRecord:
        created_at = datetime_from_row(row["created_at"])
        if created_at is None:
            raise ValueError("Agent deletion request creation timestamp is required")
        cancel = str_from_row(row.get("cancel_reason"))
        return cls(
            id=uuid_from_row(row["id"]),
            tenant_id=uuid_from_row(row["tenant_id"]),
            requester_id=uuid_from_row(row["requester_id"]),
            agent_id=uuid_from_row(row["agent_id"]),
            agent_name=str_from_row(row["agent_name"]),
            agent_creator_id=uuid_from_row(row["agent_creator_id"]),
            reason=str_from_row(row["reason"]),
            created_at=created_at,
            status=DeletionStatus(str_from_row(row["status"])),
            cancel_reason=CancelReason(cancel) if cancel else None,
            resolved_at=datetime_from_row(row.get("resolved_at")),
        )
