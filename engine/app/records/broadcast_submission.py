"""Idempotency receipt for an accepted admin broadcast."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from pydantic import TypeAdapter


@dataclass(frozen=True, slots=True)
class BroadcastSubmissionRecord:
    id: UUID
    tenant_id: UUID
    sender_id: UUID
    idempotency_key: str
    fingerprint: str
    broadcast_id: UUID
    users_notified: int
    agents_notified: int
    emails_sent: int
    created_at: datetime

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> BroadcastSubmissionRecord:
        return _RECORD_ADAPTER.validate_python(dict(row))


_RECORD_ADAPTER = TypeAdapter(BroadcastSubmissionRecord)


@dataclass(frozen=True, slots=True)
class BroadcastSubmissionClaim:
    """Outcome of claiming a key: ``created`` is False when a committed receipt already owns it."""

    submission: BroadcastSubmissionRecord
    created: bool
