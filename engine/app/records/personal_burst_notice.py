"""Cooldown metadata for the static "not queued" notice sources."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal
from uuid import UUID

from pydantic import TypeAdapter

from app.core.json_types import uuid_from_row
from app.records.personal_burst import PersonalBurstScope, required_datetime

BusyNoticeReason = Literal["capacity", "quota", "oversize"]
BUSY_NOTICE_ROUTING_REASON: Final = "whatsapp_busy"
_REASON: Final = TypeAdapter(BusyNoticeReason)


@dataclass(frozen=True, slots=True)
class PersonalBusyNotice:
    id: UUID
    gateway_id: UUID
    scope: PersonalBurstScope
    reason: BusyNoticeReason
    created_at: datetime

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> PersonalBusyNotice:
        return cls(
            id=uuid_from_row(row["id"]),
            gateway_id=uuid_from_row(row["gateway_id"]),
            scope=PersonalBurstScope.from_row(row),
            reason=_REASON.validate_python(row["reason"]),
            created_at=required_datetime(row["created_at"]),
        )
