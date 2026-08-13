"""Participant records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(slots=True)
class ParticipantRecord:
    """Unified participant identity for users and agents."""

    id: UUID
    type: str
    ref_id: UUID
    display_name: str
    avatar_url: str | None = None
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> ParticipantRecord:
        return cls(
            id=row["id"],
            type=row["type"],
            ref_id=row["ref_id"],
            display_name=row["display_name"],
            avatar_url=row.get("avatar_url"),
            created_at=row.get("created_at"),
        )
