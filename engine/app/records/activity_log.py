"""Agent activity log records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from app.core.domain.activity import ActivityAction
from app.core.json_types import (
    JsonObject,
    JsonValue,
    datetime_from_row,
    json_object_from,
    str_from_row,
    uuid_from_row,
    uuid_from_row_opt,
)


@dataclass(slots=True)
class AgentActivityLogRecord:
    """One recorded agent action for activity / audit UI."""

    id: UUID
    agent_id: UUID
    action_type: ActivityAction
    summary: str
    detail_json: JsonObject = field(default_factory=dict[str, JsonValue])
    related_id: UUID | None = None
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> AgentActivityLogRecord:
        detail = json_object_from(row.get("detail_json") or {})
        return cls(
            id=uuid_from_row(row["id"]),
            agent_id=uuid_from_row(row["agent_id"]),
            action_type=ActivityAction(str_from_row(row["action_type"])),
            summary=str_from_row(row.get("summary")),
            detail_json=detail,
            related_id=uuid_from_row_opt(row.get("related_id")),
            created_at=datetime_from_row(row.get("created_at")),
        )
