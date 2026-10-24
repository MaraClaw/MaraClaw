"""Immutable broadcast recipient snapshots and durable delivery state."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import TypeAdapter


@dataclass(frozen=True, slots=True)
class NewBroadcastEmail:
    broadcast_id: UUID
    tenant_id: UUID
    user_id: UUID
    to_email: str
    subject: str
    body: str


@dataclass(frozen=True, slots=True)
class BroadcastEmailRecord:
    id: UUID
    tenant_id: UUID
    broadcast_id: UUID
    user_id: UUID
    to_email: str
    subject: str
    body: str
    message_id: str
    status: Literal["pending", "sent", "failed", "cancelled"]
    attempts: int
    available_at: datetime
    deliver_by: datetime
    claim_token: UUID | None
    lease_expires_at: datetime | None
    last_error: str | None
    created_at: datetime
    settled_at: datetime | None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> BroadcastEmailRecord:
        return _RECORD_ADAPTER.validate_python(dict(row))


_RECORD_ADAPTER = TypeAdapter(BroadcastEmailRecord)


@dataclass(frozen=True, slots=True)
class BroadcastEmailStatus:
    broadcast_id: UUID
    available: bool
    queued: int
    claimed: int
    smtp_accepted: int
    failed: int
    cancelled: int

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> BroadcastEmailStatus:
        return _STATUS_ADAPTER.validate_python(dict(row))


@dataclass(frozen=True, slots=True)
class BroadcastEmailCancellation:
    status: BroadcastEmailStatus
    cancelled_now: int


_STATUS_ADAPTER = TypeAdapter(BroadcastEmailStatus)
