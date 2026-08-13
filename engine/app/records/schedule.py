"""Agent schedule records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


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
    def from_row(cls, row: dict[str, Any]) -> AgentScheduleRecord:
        return cls(
            id=row["id"],
            agent_id=row["agent_id"],
            name=row["name"],
            instruction=row.get("instruction") or "",
            cron_expr=row["cron_expr"],
            created_by=row["created_by"],
            is_enabled=bool(row.get("is_enabled", True)),
            last_run_at=row.get("last_run_at"),
            next_run_at=row.get("next_run_at"),
            run_count=int(row.get("run_count") or 0),
            created_at=row.get("created_at"),
        )
