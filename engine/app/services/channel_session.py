"""Shared helper: find-or-create ChatSession by external channel conv_id.

Used by feishu.py, slack.py, discord_bot.py, wecom.py, teams.py - eliminates in-process caches.
"""

from __future__ import annotations

import uuid as _uuid
from datetime import UTC, datetime
from typing import Any

from app.dao.chat_dao import chat_session_dao
from app.db.errors import UniqueViolationError
from app.records.chat import ChatSessionRecord


async def find_or_create_channel_session(
    db: Any,
    agent_id: _uuid.UUID,
    user_id: _uuid.UUID,
    external_conv_id: str,
    source_channel: str,
    first_message_title: str,
    is_group: bool = False,
    group_name: str | None = None,
) -> ChatSessionRecord:
    """Find an existing ChatSession by (agent_id, external_conv_id), or create one.

    Relies on the UNIQUE constraint on (agent_id, external_conv_id) in the DB.

    ``db`` is accepted for call-site compatibility and ignored (psycopg path).
    """
    del db
    session = await chat_session_dao.get_by_external_conv(
        agent_id=agent_id,
        external_conv_id=external_conv_id,
    )

    if session is None:
        now = datetime.now(UTC)
        try:
            return await chat_session_dao.create(
                obj_in={
                    "agent_id": agent_id,
                    "user_id": user_id,
                    "title": group_name[:40] if (is_group and group_name) else first_message_title[:40],
                    "source_channel": source_channel,
                    "external_conv_id": external_conv_id,
                    "is_group": is_group,
                    "group_name": group_name,
                    "created_at": now,
                }
            )
        except UniqueViolationError:
            session = await chat_session_dao.get_by_external_conv(
                agent_id=agent_id,
                external_conv_id=external_conv_id,
            )
            if session is None:
                raise

    updates: dict[str, Any] = {}
    # For P2P sessions: re-attribute to the correct user
    # (fixes legacy sessions stored under creator_id)
    if not session.is_group and session.user_id != user_id:
        updates["user_id"] = user_id

    # For group sessions: update group_name if it changed
    if session.is_group and group_name and session.group_name != group_name:
        updates["group_name"] = group_name
        updates["title"] = group_name[:40]

    if updates:
        return await chat_session_dao.update(db_obj=session, obj_in=updates)
    return session
