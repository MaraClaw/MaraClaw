"""Agent-to-agent relationship records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


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

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> AgentAgentRelationshipRecord:
        return cls(
            id=row["id"],
            agent_id=row["agent_id"],
            target_agent_id=row["target_agent_id"],
            relation=row.get("relation") or "collaborator",
            description=row.get("description") or "",
            created_by_user_id=row.get("created_by_user_id"),
            updated_by_user_id=row.get("updated_by_user_id"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )
