"""Agent schedule records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.core.json_types import datetime_from_row, int_from_row, str_from_row, uuid_from_row


@dataclass(slots=True)
class AgentScheduleRecord:
    """Cron-based autonomous task schedule for an agent."""

    id: UUID
    agent_id: UUID
    name: str
    instruction: str
    cron_expr: str
    created_by: UUID
    is_enabled: bool = True
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    run_count: int = 0
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> AgentScheduleRecord:
        return cls(
            id=uuid_from_row(row["id"]),
            agent_id=uuid_from_row(row["agent_id"]),
            name=str_from_row(row["name"]),
            instruction=str_from_row(row.get("instruction")),
            cron_expr=str_from_row(row["cron_expr"]),
            created_by=uuid_from_row(row["created_by"]),
            is_enabled=bool(row.get("is_enabled", True)),
            last_run_at=datetime_from_row(row.get("last_run_at")),
            next_run_at=datetime_from_row(row.get("next_run_at")),
            run_count=int_from_row(row.get("run_count")),
            created_at=datetime_from_row(row.get("created_at")),
        )
