"""Scoped keyset history with bounded, fail-open Redis response caching."""

import base64
import binascii
from typing import ClassVar, Literal, TypedDict
from uuid import UUID

from fastapi import HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, TypeAdapter, ValidationError

from app.core.redis_cache import cache_get_json, cache_key, cache_set_json
from app.dao.chat_dao import chat_message_dao
from app.records.chat import ChatSessionRecord
from app.services.chat_message_projection import ChatMessageEntry, project_messages


class HistoryCursor(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1] = 1
    agent_id: UUID
    session_id: UUID
    created_at: AwareDatetime
    message_id: UUID

    def encode(self) -> str:
        return base64.urlsafe_b64encode(self.model_dump_json().encode()).decode().rstrip("=")


class ChatMessagePage(TypedDict):
    items: list[ChatMessageEntry]
    next_cursor: str | None
    has_more: bool


_PAGE_ADAPTER = TypeAdapter(ChatMessagePage)


def parse_cursor(raw: str | None, agent_id: UUID, session_id: UUID) -> HistoryCursor | None:
    if raw is None:
        return None
    try:
        decoded = base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True)
        cursor = HistoryCursor.model_validate_json(decoded)
    except binascii.Error, ValueError:
        raise HTTPException(400, "Invalid history cursor") from None
    if cursor.agent_id != agent_id or cursor.session_id != session_id:
        raise HTTPException(400, "History cursor belongs to another agent or session")
    return cursor


async def get_history_page(
    session: ChatSessionRecord, limit: int, cursor: HistoryCursor | None, *, agent_id: UUID
) -> ChatMessagePage:
    # Durable mutation fingerprint: no COUNT, history scan, or Redis invalidation
    # dependency. An in-flight stale fill stays under its old, unreachable key.
    revision = await chat_message_dao.history_revision(session.id)
    key = cache_key(
        "chat-history",
        "v1",
        str(agent_id),
        str(session.id),
        session.source_channel,
        str(revision),
        str(limit),
        cursor.encode() if cursor else "latest",
    )
    cached = await cache_get_json(key) if revision is not None else None
    if cached is not None:
        try:
            return _PAGE_ADAPTER.validate_python(cached)
        except ValidationError:
            pass  # A corrupt cache value is a miss, never a failed history read.

    messages = list(
        await chat_message_dao.list_session_page(
            conversation_id=str(session.id),
            limit=limit + 1,
            before=(cursor.created_at, cursor.message_id) if cursor else None,
        )
    )
    has_more = len(messages) > limit
    selected = messages[:limit]
    next_cursor = None
    if has_more:
        oldest = selected[-1]
        if oldest.created_at is None:
            raise ValueError("Persisted chat message has no creation timestamp")
        next_cursor = HistoryCursor(
            agent_id=agent_id,
            session_id=session.id,
            created_at=oldest.created_at,
            message_id=oldest.id,
        ).encode()
    result: ChatMessagePage = {
        "items": await project_messages(list(reversed(selected)), session.source_channel),
        "next_cursor": next_cursor,
        "has_more": has_more,
    }
    if revision is not None:
        _ = await cache_set_json(key, result, ttl=30)
    return result
