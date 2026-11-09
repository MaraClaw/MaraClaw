"""Immutable broadcast draft and committed fanout progress."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from pydantic import TypeAdapter

from app.core.domain.notifications import BroadcastJobStatus


@dataclass(frozen=True, slots=True)
class BroadcastJobDraft:
    tenant_id: UUID
    sender_id: UUID
    sender_name: str
    idempotency_key: str
    title: str
    body: str
    send_email: bool


@dataclass(frozen=True, slots=True)
class BroadcastJobRecord:
    broadcast_id: UUID
    tenant_id: UUID
    sender_id: UUID | None
    sender_scope_id: UUID
    sender_name: str
    idempotency_key: str
    title: str
    body: str
    send_email: bool
    accepted_at: datetime
    completed_at: datetime | None
    users_total: int
    agents_total: int
    cursor: int
    users_notified: int
    agents_notified: int
    skipped: int
    emails_queued: int
    status: BroadcastJobStatus

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> BroadcastJobRecord:
        return _RECORD.validate_python(dict(row))


_RECORD = TypeAdapter(BroadcastJobRecord)
