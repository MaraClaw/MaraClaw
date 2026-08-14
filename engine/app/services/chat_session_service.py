"""Helpers for first-party chat session selection and creation."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from app.core.json_types import JsonObject
from app.core.logging import logger
from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.db.session import connection_ctx
from app.records.chat import ChatSessionRecord


async def get_primary_platform_session(
    db: object | None,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
) -> ChatSessionRecord | None:
    """Return the current primary first-party session for a user+agent pair, if any."""
    del db
    return await chat_session_dao.get_primary_platform(agent_id=agent_id, user_id=user_id)


async def ensure_primary_platform_session(
    db: object | None,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
) -> ChatSessionRecord:
    """Return a guaranteed primary platform session for a given user+agent pair.

    The upgrade strategy is intentionally lazy:
    - Reuse the existing primary session when it exists.
    - Otherwise promote the most relevant existing web session.
    - Only create a brand new primary session when the pair has never talked on-platform.
    """
    del db

    primary = await get_primary_platform_session(None, agent_id, user_id)
    if primary:
        return primary

    existing = await chat_session_dao.find_best_web_session(agent_id=agent_id, user_id=user_id)
    if existing:
        return await chat_session_dao.update(db_obj=existing, obj_in={"is_primary": True})

    now = datetime.now(UTC)
    return await chat_session_dao.create(
        obj_in={
            "agent_id": agent_id,
            "user_id": user_id,
            "title": f"Session {now.strftime('%m-%d %H:%M')}",
            "source_channel": "web",
            "is_primary": True,
            "created_at": now,
        }
    )


async def save_tool_call_log(
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: str,
    tool_name: str,
    arguments: JsonObject | None,
    result: str,
    status: str = "done",
    tool_call_id: str | None = None,
    reasoning_content: str | None = None,
) -> None:
    """Save a tool call execution log into chat history as a ChatMessage."""
    if not conversation_id:
        return

    payload: dict[str, Any] = {
        "name": tool_name,
        "args": arguments or {},
        "status": status,
        "result": str(result) if result is not None else "",
        "tool_call_id": tool_call_id,
        "reasoning_content": reasoning_content,
    }

    try:
        # Open a short-lived connection when no request-scoped transaction is active.
        async with connection_ctx():
            _ = await chat_message_dao.insert_message(
                agent_id=agent_id,
                user_id=user_id,
                role="tool_call",
                content=json.dumps(payload, ensure_ascii=False, default=str),
                conversation_id=conversation_id,
            )
    except Exception as e:
        logger.warning(f"Failed to save tool call log: {e}")
