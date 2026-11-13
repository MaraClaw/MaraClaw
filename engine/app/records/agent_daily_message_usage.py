"""Per-agent daily human-message counter row."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from app.core.json_types import date_from_row, datetime_from_row, int_from_row, uuid_from_row


@dataclass(slots=True)
class AgentDailyMessageUsageRecord:
    agent_id: UUID
    usage_date: date
    messages_used: int
    updated_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> AgentDailyMessageUsageRecord:
        return cls(
            agent_id=uuid_from_row(row["agent_id"]),
            usage_date=date_from_row(row["usage_date"]),
            messages_used=int_from_row(row["messages_used"]),
            updated_at=datetime_from_row(row.get("updated_at")),
        )
