"""Participant records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.core.json_types import datetime_from_row, str_from_row, uuid_from_row


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
    def from_row(cls, row: Mapping[str, object]) -> ParticipantRecord:
        return cls(
            id=uuid_from_row(row["id"]),
            type=str_from_row(row["type"]),
            ref_id=uuid_from_row(row["ref_id"]),
            display_name=str_from_row(row["display_name"]),
            avatar_url=str_from_row(row["avatar_url"]) or None,
            created_at=datetime_from_row(row.get("created_at")),
        )
