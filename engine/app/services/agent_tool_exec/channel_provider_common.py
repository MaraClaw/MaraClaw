from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import ModuleType
from typing import Any

from app.core.logging import logger
from app.dao.agent_dao import agent_dao
from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.services.channel_session import find_or_create_channel_session
from app.services.channel_user_service import get_platform_user_by_org_member


async def _save_channel_message(
    facade: ModuleType | None = None,
    *,
    db: Any = None,
    agent_id: uuid.UUID,
    org_member: Any,
    external_conv_id: str,
    source_channel: str,
    message_text: str,
    log_label: str,
) -> None:
    """Persist a proactive outbound channel message. ``db`` is dual-stack and ignored."""
    del db
    agent = await agent_dao.get(agent_id)
    platform_user = await get_platform_user_by_org_member(
        db=None,
        org_member=org_member,
        agent_tenant_id=agent.tenant_id if agent else None,
    )
    session = await find_or_create_channel_session(
        db=None,
        agent_id=agent_id,
        user_id=platform_user.id,
        external_conv_id=external_conv_id,
        source_channel=source_channel,
        first_message_title=message_text[:30],
    )
    await chat_message_dao.insert_message(
        agent_id=agent_id,
        user_id=platform_user.id,
        role="assistant",
        content=message_text,
        conversation_id=str(session.id),
    )
    await chat_session_dao.update(db_obj=session, obj_in={"last_message_at": datetime.now(UTC)})
    log = getattr(facade, "logger", None) if facade is not None else None
    if log is not None:
        log.info(f"[{log_label}] Proactive message saved to session {session.id}")
    else:
        logger.info(f"[{log_label}] Proactive message saved to session {session.id}")
