"""Helpers for first-party chat session selection and creation."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any, cast

from app.core.json_types import JsonObject
from app.core.logging import logger
from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.db.session import connection_ctx
from app.records.chat import ChatSessionRecord
from app.services.shared_chat import ensure_shared_session, is_shared_session, shared_session_id


async def get_primary_platform_session(
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
) -> ChatSessionRecord | None:
    """Return the canonical first-party session, without promoting private history."""
    session = await chat_session_dao.get_for_agent(shared_session_id(agent_id), agent_id)
    return session if session is not None and is_shared_session(session) else None


async def ensure_primary_platform_session(
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    guard: Callable[[], None] | None = None,
) -> ChatSessionRecord:
    """Resolve the agent's shared human session; user_id remains caller provenance only."""
    if guard is not None:
        guard()
        return await ensure_shared_session(agent_id, guard=guard)
    return await ensure_shared_session(agent_id)


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
        "result": str(result) if cast(object, result) is not None else "",
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
