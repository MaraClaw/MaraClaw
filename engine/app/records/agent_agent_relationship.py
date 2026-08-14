"""Agent-to-agent relationship records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from app.core.json_types import datetime_from_row, str_from_row, uuid_from_row, uuid_from_row_opt

if TYPE_CHECKING:
    from app.records.agent import AgentRecord


@dataclass(slots=True)
class AgentAgentRelationshipRecord:
    """Link between two agents."""

    id: UUID
    agent_id: UUID
    target_agent_id: UUID
    relation: str = "collaborator"
    description: str = ""
    created_by_user_id: UUID | None = None
    updated_by_user_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    target_agent: AgentRecord | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> AgentAgentRelationshipRecord:
        return cls(
            id=uuid_from_row(row["id"]),
            agent_id=uuid_from_row(row["agent_id"]),
            target_agent_id=uuid_from_row(row["target_agent_id"]),
            relation=str_from_row(row.get("relation"), "collaborator") or "collaborator",
            description=str_from_row(row.get("description")),
            created_by_user_id=uuid_from_row_opt(row.get("created_by_user_id")),
            updated_by_user_id=uuid_from_row_opt(row.get("updated_by_user_id")),
            created_at=datetime_from_row(row.get("created_at")),
            updated_at=datetime_from_row(row.get("updated_at")),
        )
