"""Published admin broadcast, as shown in the tenant's broadcast history."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from pydantic import TypeAdapter


@dataclass(frozen=True, slots=True)
class BroadcastHistoryRecord:
    broadcast_id: UUID
    tenant_id: UUID
    sender_id: UUID | None
    sender_name: str
    title: str
    body: str
    send_email: bool
    users_notified: int
    agents_notified: int
    emails_sent: int
    published_at: datetime

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> BroadcastHistoryRecord:
        return _RECORD_ADAPTER.validate_python(dict(row))


_RECORD_ADAPTER = TypeAdapter(BroadcastHistoryRecord)


@dataclass(frozen=True, slots=True)
class NewBroadcastHistory:
    broadcast_id: UUID
    tenant_id: UUID
    sender_id: UUID
    sender_name: str
    title: str
    body: str
    send_email: bool
    users_notified: int
    agents_notified: int
    emails_sent: int
