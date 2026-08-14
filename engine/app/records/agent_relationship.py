"""Agent-org-member relationship records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from app.records.org import OrgMemberRecord


@dataclass(slots=True)
class AgentRelationshipRecord:
    """Link between an agent and an org member."""

    id: UUID
    agent_id: UUID
    member_id: UUID
    relation: str = "collaborator"
    description: str = ""
    created_by_user_id: UUID | None = None
    updated_by_user_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    member: OrgMemberRecord | None = None
    provider_name: str | None = None
    provider_type: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> AgentRelationshipRecord:
        return cls(
            id=row["id"],
            agent_id=row["agent_id"],
            member_id=row["member_id"],
            relation=row.get("relation") or "collaborator",
            description=row.get("description") or "",
            created_by_user_id=row.get("created_by_user_id"),
            updated_by_user_id=row.get("updated_by_user_id"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )
