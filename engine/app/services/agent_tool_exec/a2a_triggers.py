from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TypedDict

from app.core.logging import logger
from app.dao.trigger_dao import agent_trigger_dao
from app.db.session import connection_ctx
from app.services.focus_service import ensure_focus_item


class _WakeAgentOptions(TypedDict):
    from_agent_id: uuid.UUID | None
    skip_dedup: bool
    a2a_session_id: str | None


async def _create_on_message_trigger(
    agent_id: uuid.UUID,
    trigger_name: str,
    from_agent_name: str,
    reason: str,
    focus_ref: str | None = None,
    notification_summary: str | None = None,
    origin_session_id: str | None = None,
    origin_user_id: str | None = None,
    origin_source_channel: str | None = None,
) -> None:
    """Programmatically create an on_message trigger for an agent."""
    focus_ref = await ensure_focus_item(agent_id, focus_ref=focus_ref, description=reason or trigger_name)

    config: dict[str, str] = {"from_agent_name": from_agent_name}
    if notification_summary:
        config["_notification_summary"] = notification_summary
    if origin_session_id:
        config["_origin_session_id"] = origin_session_id
    if origin_user_id:
        config["_origin_user_id"] = origin_user_id
    if origin_source_channel:
        config["_origin_source_channel"] = origin_source_channel

    from contextlib import suppress

    with suppress(Exception):
        async with connection_ctx() as db:
            value = await db.fetchval(
                "SELECT m.created_at FROM chat_messages m "
                + "JOIN chat_sessions s ON m.conversation_id = s.id::text "
                + "WHERE s.agent_id = %(agent_id)s AND m.created_at IS NOT NULL "
                + "ORDER BY m.created_at DESC LIMIT 1",
                {"agent_id": agent_id},
            )
            if value:
                config["_since_ts"] = value.isoformat()

    existing = await agent_trigger_dao.get_by_agent_and_name(agent_id, trigger_name)
    if existing:
        if existing.is_enabled:
            _ = await agent_trigger_dao.update(
                db_obj=existing,
                obj_in={
                    "config": {**(existing.config or {}), **config},
                    "reason": reason,
                    "fire_count": 0,
                    **({"focus_ref": focus_ref} if focus_ref else {}),
                },
            )
            return
        _ = await agent_trigger_dao.update(
            db_obj=existing,
            obj_in={
                "type": "on_message",
                "config": config,
                "reason": reason,
                "focus_ref": focus_ref or None,
                "is_enabled": True,
                "fire_count": 0,
            },
        )
        return

    _ = await agent_trigger_dao.create(
        obj_in={
            "agent_id": agent_id,
            "name": trigger_name,
            "type": "on_message",
            "config": config,
            "reason": reason,
            "focus_ref": focus_ref or None,
            "max_fires": 1,
            "expires_at": datetime.now(UTC) + timedelta(hours=24),
        }
    )


async def _append_focus_item(agent_id: uuid.UUID, identifier: str, description: str) -> None:
    """Create or update an in-progress Focus item."""
    try:
        _ = await ensure_focus_item(agent_id, focus_ref=identifier, description=description)
    except Exception as error:
        logger.warning(f"[A2A] Failed to update Focus for agent {agent_id}: {error}")


async def _wake_agent_async(
    agent_id: uuid.UUID,
    reason_context: str,
    *,
    from_agent_id: uuid.UUID | None = None,
    skip_dedup: bool = False,
    a2a_session_id: str | None = None,
) -> None:
    """Wake an agent asynchronously via the trigger invocation path."""
    from app.services.trigger_daemon import wake_agent_with_context

    kwargs: _WakeAgentOptions = {
        "from_agent_id": from_agent_id,
        "skip_dedup": skip_dedup,
        "a2a_session_id": a2a_session_id,
    }
    await wake_agent_with_context(agent_id, reason_context, **kwargs)
