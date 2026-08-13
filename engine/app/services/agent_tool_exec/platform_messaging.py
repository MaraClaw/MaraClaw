import uuid
from datetime import UTC, datetime

from app.core.logging import logger
from app.core.permissions import evaluate_human_relationship_status
from app.dao.agent_dao import agent_dao
from app.dao.agent_relationship_dao import agent_relationship_dao
from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.dao.user_dao import user_dao
from app.services.access_relationships import ensure_access_granted_platform_relationships

from .registry import ToolArgumentMapping


async def _send_platform_message(agent_id: uuid.UUID, args: ToolArgumentMapping) -> str:
    """Send a proactive message to a first-party platform user."""
    username = _string_argument(args, "username")
    message_text = _string_argument(args, "message")

    if not username or not message_text:
        return "❌ Please provide recipient username and message content"

    try:
        agent = await agent_dao.get(agent_id)
        if not agent:
            return "❌ Agent not found"
        await ensure_access_granted_platform_relationships(None, agent, created_by_user_id=agent.creator_id)

        # 1. Look up target user by username or display_name within tenant
        target_user = await user_dao.find_by_username_or_display_name(
            username, tenant_id=agent.tenant_id, include_identity=True
        )
        if not target_user:
            names: list[str] = []
            if agent.tenant_id:
                names = await user_dao.list_display_names_for_tenant(agent.tenant_id, limit=20)
            return (
                f"❌ No user named '{username}' found in your organization. "
                f"Available users: {', '.join(names) if names else 'none'}"
            )

        rel = await agent_relationship_dao.get_for_agent_and_user(agent_id, target_user.id)
        if not rel:
            return f"❌ {target_user.display_name or target_user.username} is not in your active relationship network"
        status_info = await evaluate_human_relationship_status(None, rel, source_agent=agent)
        if status_info["access_status"] != "active":
            return (
                f"❌ Relationship to {target_user.display_name or target_user.username} "
                f"is not active ({status_info['access_status_reason'] or 'restricted'})"
            )

        # Agent-initiated platform messages should always go to the long-lived primary session
        from app.services.chat_session_service import ensure_primary_platform_session

        session = await ensure_primary_platform_session(None, agent_id, target_user.id)

        await chat_message_dao.insert_message(
            agent_id=agent_id,
            user_id=target_user.id,
            role="assistant",
            content=message_text,
            conversation_id=str(session.id),
        )
        now = datetime.now(UTC)
        await chat_session_dao.update(db_obj=session, obj_in={"last_message_at": now})
        try:
            from app.api.websocket import maybe_mark_session_read_for_active_viewer

            await maybe_mark_session_read_for_active_viewer(
                None,
                agent_id=agent_id,
                session_id=str(session.id),
                user_id=target_user.id,
            )
        except Exception:
            logger.debug("Platform message read marker failed")

        # Push via WebSocket if user has an active connection
        try:
            from app.api.websocket import manager as ws_manager

            await ws_manager.send_to_user(
                str(agent_id),
                str(target_user.id),
                {
                    "type": "trigger_notification",
                    "content": message_text,
                    "triggers": ["web_message"],
                    "session_id": str(session.id),
                },
            )
        except Exception:
            logger.debug("Platform message websocket notification failed")

        display = target_user.display_name or target_user.username
        return f"✅ Message sent to {display} on web platform. It has been saved to their chat history."

    except Exception as e:
        logger.exception("[PlatformMessage] Error")
        return f"❌ Web message send error: {str(e)[:200]}"


def _string_argument(arguments: ToolArgumentMapping, name: str) -> str:
    value = arguments.get(name)
    return value.strip() if isinstance(value, str) else ""
