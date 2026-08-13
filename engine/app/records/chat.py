"""Chat session and message records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


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
    def from_row(cls, row: dict[str, Any]) -> ChatSessionRecord:
        return cls(
            id=row["id"],
            agent_id=row["agent_id"],
            user_id=row["user_id"],
            title=row.get("title") or "New Session",
            source_channel=row.get("source_channel") or "web",
            external_conv_id=row.get("external_conv_id"),
            is_group=bool(row.get("is_group", False)),
            group_name=row.get("group_name"),
            participant_id=row.get("participant_id"),
            peer_agent_id=row.get("peer_agent_id"),
            is_primary=bool(row.get("is_primary", False)),
            last_read_at_by_user=row.get("last_read_at_by_user"),
            created_at=row.get("created_at"),
            last_message_at=row.get("last_message_at"),
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
    def from_row(cls, row: dict[str, Any]) -> ChatMessageRecord:
        return cls(
            id=row["id"],
            agent_id=row["agent_id"],
            user_id=row["user_id"],
            role=row["role"],
            content=row.get("content") or "",
            conversation_id=row.get("conversation_id") or "web",
            participant_id=row.get("participant_id"),
            thinking=row.get("thinking"),
            created_at=row.get("created_at"),
        )
