"""Scoped keyset history with bounded, fail-open Redis response caching."""

import base64
import binascii
from typing import Annotated, ClassVar, Literal, TypedDict
from uuid import UUID

from fastapi import HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

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


class HeldHistoryQuery(BaseModel):
    """Raw refresh boundary: the revision and newest message a client holds, never a projected entry id."""

    revision: str | None = Field(None, max_length=64)
    after_created_at: str | None = Field(None, max_length=64)
    after_message_id: str | None = Field(None, max_length=36)


class HeldHistory(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    revision: str = Field(min_length=1, max_length=64)
    created_at: AwareDatetime
    message_id: UUID


class ChatMessagePage(TypedDict):
    items: list[ChatMessageEntry]
    next_cursor: str | None
    has_more: bool
    revision: str | None


class ChatHistoryUnchanged(TypedDict):
    kind: Literal["unchanged"]
    revision: str


class ChatHistoryDelta(TypedDict):
    kind: Literal["delta"]
    revision: str | None
    items: list[ChatMessageEntry]


class ChatHistoryReset(TypedDict):
    kind: Literal["reset"]
    page: ChatMessagePage


ChatHistoryRefresh = Annotated[ChatHistoryUnchanged | ChatHistoryDelta | ChatHistoryReset, Field(discriminator="kind")]

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


def parse_held(query: HeldHistoryQuery) -> HeldHistory | None:
    raw = (query.revision, query.after_created_at, query.after_message_id)
    if all(part is None for part in raw):
        return None
    try:
        return HeldHistory.model_validate(
            {"revision": query.revision, "created_at": query.after_created_at, "message_id": query.after_message_id}
        )
    except ValidationError:
        raise HTTPException(400, "Invalid history refresh boundary") from None


async def get_history_page(
    session: ChatSessionRecord, limit: int, cursor: HistoryCursor | None, *, agent_id: UUID
) -> ChatMessagePage:
    # Durable mutation fingerprint: no COUNT, history scan, or Redis invalidation
    # dependency. An in-flight stale fill stays under its old, unreachable key.
    revision = await chat_message_dao.history_revision(session.id)
    return await read_history_page(session, limit, cursor, agent_id=agent_id, revision=revision)


async def get_history_refresh(
    session: ChatSessionRecord, limit: int, held: HeldHistory, *, agent_id: UUID
) -> ChatHistoryUnchanged | ChatHistoryDelta | ChatHistoryReset:
    """Conditional latest read against the revision and newest message boundary a client holds.

    Read the revision before rows and return that same revision. The transactional UUID
    predecessor journal identifies late inserts/updates regardless of timestamp. A bounded
    walk must prove completeness; deletions, missing ancestry and oversized gaps reset.
    """
    revision = await chat_message_dao.history_revision(session.id)
    if revision is not None and revision == held.revision:  # A missing revision is always a change.
        return {"kind": "unchanged", "revision": revision}
    changed = (
        await chat_message_dao.list_session_changes(
            session_id=session.id, revision=revision, since=held.revision, limit=limit
        )
        if revision is not None
        else None
    )
    if changed is None:
        return {
            "kind": "reset",
            "page": await read_history_page(session, limit, None, agent_id=agent_id, revision=revision),
        }
    conversation_id = str(session.id)
    boundary = (held.created_at, held.message_id)
    newer = list(
        await chat_message_dao.list_session_after(conversation_id=conversation_id, after=boundary, limit=limit + 1)
    )
    if not newer or newer[0].id != held.message_id or len(newer) > limit:
        page = await read_history_page(session, limit, None, agent_id=agent_id, revision=revision)
        return {"kind": "reset", "page": page}
    messages = {message.id: message for message in [*newer, *changed]}
    if len(messages) > limit:
        return {
            "kind": "reset",
            "page": await read_history_page(session, limit, None, agent_id=agent_id, revision=revision),
        }
    return {
        "kind": "delta",
        "revision": revision,
        "items": await project_messages(
            sorted(messages.values(), key=lambda message: (message.created_at, message.id)), session.source_channel
        ),
    }


async def read_history_page(
    session: ChatSessionRecord, limit: int, cursor: HistoryCursor | None, *, agent_id: UUID, revision: str | None
) -> ChatMessagePage:
    key = cache_key(
        "chat-history",
        "v2",
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
        "revision": revision,
    }
    if revision is not None:
        _ = await cache_set_json(key, result, ttl=30)
    return result
