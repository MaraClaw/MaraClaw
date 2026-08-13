"""One-connection persist helpers used after an LLM turn (not during it)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from app.dao.agent_dao import agent_dao
from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.db.session import connection_ctx


async def persist_chat_message(
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: str,
    role: str,
    content: str,
    thinking: str | None = None,
    participant_id: uuid.UUID | None = None,
    session_updates: dict[str, Any] | None = None,
    title_if_default: str | None = None,
    touch_last_active: bool = False,
    skip_insert: bool = False,
    agent: Any | None = None,
) -> None:
    """Insert a chat message and optionally touch session / agent on one checkout."""
    async with connection_ctx():
        if not skip_insert:
            await chat_message_dao.insert_message(
                agent_id=agent_id,
                user_id=user_id,
                role=role,
                content=content,
                conversation_id=conversation_id,
                participant_id=participant_id,
                thinking=thinking,
            )
        try:
            session_id = uuid.UUID(str(conversation_id))
        except ValueError, TypeError:
            session_id = None
        if session_id is not None:
            session = await chat_session_dao.get(session_id)
            if session is not None:
                updates: dict[str, Any] = {}
                if not skip_insert:
                    updates["last_message_at"] = datetime.now(UTC)
                if session_updates:
                    updates.update(session_updates)
                if title_if_default and str(getattr(session, "title", "") or "").startswith("Session "):
                    updates["title"] = title_if_default
                if updates:
                    await chat_session_dao.update(db_obj=session, obj_in=updates)
        if touch_last_active:
            row = agent if agent is not None else await agent_dao.get(agent_id)
            if row is not None:
                await agent_dao.update(db_obj=row, obj_in={"last_active_at": datetime.now(UTC)})
