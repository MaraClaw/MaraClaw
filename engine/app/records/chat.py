"""Chat session and message records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.core.json_types import datetime_from_row, str_from_row, uuid_from_row, uuid_from_row_opt


@dataclass(slots=True)
class ChatSessionRecord:
    """Named session grouping chat messages between a user and an agent."""

    id: UUID
    agent_id: UUID
    user_id: UUID
    title: str = "New Session"
    source_channel: str = "web"
    external_conv_id: str | None = None
    is_group: bool = False
    group_name: str | None = None
    participant_id: UUID | None = None
    peer_agent_id: UUID | None = None
    is_primary: bool = False
    last_read_at_by_user: datetime | None = None
    created_at: datetime | None = None
    last_message_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> ChatSessionRecord:
        return cls(
            id=uuid_from_row(row["id"]),
            agent_id=uuid_from_row(row["agent_id"]),
            user_id=uuid_from_row(row["user_id"]),
            title=str_from_row(row.get("title"), "New Session") or "New Session",
            source_channel=str_from_row(row.get("source_channel"), "web") or "web",
            external_conv_id=str_from_row(row["external_conv_id"]) or None,
            is_group=bool(row.get("is_group", False)),
            group_name=str_from_row(row["group_name"]) or None,
            participant_id=uuid_from_row_opt(row.get("participant_id")),
            peer_agent_id=uuid_from_row_opt(row.get("peer_agent_id")),
            is_primary=bool(row.get("is_primary", False)),
            last_read_at_by_user=datetime_from_row(row.get("last_read_at_by_user")),
            created_at=datetime_from_row(row.get("created_at")),
            last_message_at=datetime_from_row(row.get("last_message_at")),
        )


@dataclass(slots=True)
class ChatMessageRecord:
    """Single chat message in a conversation."""

    id: UUID
    agent_id: UUID
    user_id: UUID
    role: str
    content: str
    conversation_id: str = "web"
    participant_id: UUID | None = None
    thinking: str | None = None
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> ChatMessageRecord:
        return cls(
            id=uuid_from_row(row["id"]),
            agent_id=uuid_from_row(row["agent_id"]),
            user_id=uuid_from_row(row["user_id"]),
            role=str_from_row(row["role"]),
            content=str_from_row(row.get("content")),
            conversation_id=str_from_row(row.get("conversation_id"), "web") or "web",
            participant_id=uuid_from_row_opt(row.get("participant_id")),
            thinking=str_from_row(row["thinking"]) or None,
            created_at=datetime_from_row(row.get("created_at")),
        )
