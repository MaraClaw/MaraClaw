"""Agent activity log records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(slots=True)
class AgentActivityLogRecord:
    """One recorded agent action for activity / audit UI."""

    id: UUID
    agent_id: UUID
    action_type: str
    summary: str
    detail_json: dict[str, Any] = field(default_factory=dict)
    related_id: UUID | None = None
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> AgentActivityLogRecord:
        detail = row.get("detail_json") or {}
        if not isinstance(detail, dict):
            detail = dict(detail) if detail else {}
        return cls(
            id=row["id"],
            agent_id=row["agent_id"],
            action_type=row["action_type"],
            summary=row.get("summary") or "",
            detail_json=detail,
            related_id=row.get("related_id"),
            created_at=row.get("created_at"),
        )
