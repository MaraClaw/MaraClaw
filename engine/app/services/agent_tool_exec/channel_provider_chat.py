from __future__ import annotations

import importlib
import uuid
from datetime import UTC, datetime
from types import ModuleType
from typing import Any

from app.core.logging import logger
from app.dao.agent_dao import agent_dao
from app.dao.channel_config_dao import channel_config_dao
from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.services import agent_tools
from app.services.channel_user_service import get_platform_user_by_org_member


def _channel_provider_common() -> ModuleType:
    return importlib.import_module("app.services.agent_tool_exec.channel_provider_common")


async def _send_slack_message(
    agent_id: uuid.UUID,
    member_name: str,
    message_text: str,
    target_member: Any,
) -> str:
    """Send proactive Slack DM via conversations.open + chat.postMessage."""
    from app.api.slack import _send_slack_messages

    try:
        config = await channel_config_dao.get_configured_for_agent(agent_id, channel_type="slack")
        if not config:
            return "❌ This agent has no Slack channel configured"
        user_id = (target_member.external_id or "").strip()
        if not user_id:
            return f"❌ {member_name} has no Slack user_id"
        bot_token = (config.app_secret or "").strip()
        if not bot_token:
            return "❌ Slack bot token is missing"

        httpx_module = importlib.import_module("httpx")
        async with httpx_module.AsyncClient(timeout=10) as client:
            open_resp = await client.post(
                "https://slack.com/api/conversations.open",
                headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json"},
                json={"users": user_id},
            )
            data = open_resp.json()
            if open_resp.status_code >= 400 or not data.get("ok"):
                err = data.get("error") or open_resp.text[:200]
                return f"❌ Slack conversations.open failed: {err}"
            channel_id = ((data.get("channel") or {}).get("id") or "").strip()

        if not channel_id:
            return f"❌ Slack DM channel unavailable for {member_name}"

        await _send_slack_messages(bot_token, channel_id, message_text)
        try:
            await _channel_provider_common()._save_channel_message(
                agent_tools,
                db=None,
                agent_id=agent_id,
                org_member=target_member,
                external_conv_id=f"slack_{channel_id}",
                source_channel="slack",
                message_text=message_text,
                log_label="Slack",
            )
        except Exception as error:
            logger.error(f"[Slack] Failed to save proactive message to session: {error}")
        return f"✅ Message sent to {member_name} via Slack"
    except Exception as error:
        logger.exception("[Slack] Error")
        return f"❌ Slack message error: {str(error)[:200]}"


async def _send_teams_channel_message(
    agent_id: uuid.UUID,
    member_name: str,
    message_text: str,
    target_member: Any,
) -> str:
    """Send proactive Teams message using the latest known conversation context."""
    from app.api.teams import _send_teams_message

    try:
        config = await channel_config_dao.get_configured_for_agent(agent_id, channel_type="microsoft_teams")
        if not config:
            return "❌ This agent has no Teams channel configured"
        service_url = str((config.extra_config or {}).get("service_url") or "").strip()
        if not service_url:
            return "❌ Teams proactive send requires an existing inbound conversation to capture service_url"

        agent = await agent_dao.get(agent_id)
        platform_user = await get_platform_user_by_org_member(
            db=None,
            org_member=target_member,
            agent_tenant_id=agent.tenant_id if agent else None,
        )
        sessions = await chat_session_dao.list_for_user(agent_id=agent_id, user_id=platform_user.id)
        session = next((s for s in sessions if s.source_channel == "microsoft_teams"), None)
        conversation_id = str(session.external_conv_id or "").strip() if session else ""
        if session is None or not conversation_id:
            return f"❌ Teams proactive send to {member_name} requires them to message the bot first"

        await _send_teams_message(
            config,
            conversation_id,
            {"type": "message", "text": message_text, "conversation": {"id": conversation_id}},
        )
        await chat_message_dao.insert_message(
            agent_id=agent_id,
            user_id=platform_user.id,
            role="assistant",
            content=message_text,
            conversation_id=str(session.id),
        )
        await chat_session_dao.update(db_obj=session, obj_in={"last_message_at": datetime.now(UTC)})
        logger.info(f"[Teams] Proactive message saved to session {session.id}")
        return f"✅ Message sent to {member_name} via Teams"
    except Exception as error:
        logger.exception("[Teams] Error")
        return f"❌ Teams message error: {str(error)[:200]}"


async def _send_google_chat_message(
    agent_id: uuid.UUID,
    member_name: str,
    message_text: str,
    target_member: Any,
) -> str:
    """Send proactive Google Chat message using the latest known space/thread context."""
    from app.services.channels.google_chat import parse_external_conv_id, send_google_chat_message

    try:
        config = await channel_config_dao.get_configured_for_agent(agent_id, channel_type="google_chat")
        if not config:
            return "❌ This agent has no Google Chat channel configured"

        agent = await agent_dao.get(agent_id)
        platform_user = await get_platform_user_by_org_member(
            db=None,
            org_member=target_member,
            agent_tenant_id=agent.tenant_id if agent else None,
        )

        # Prefer DM sessions owned by the member, then agent-scoped google_chat sessions
        # (includes creator-owned group rooms the member previously messaged).
        dm_sessions = await chat_session_dao.list_for_user(agent_id=agent_id, user_id=platform_user.id)
        session = next((s for s in dm_sessions if s.source_channel == "google_chat"), None)
        if session is None:
            channel_sessions = await chat_session_dao.list_for_agent_channel(
                agent_id=agent_id,
                source_channel="google_chat",
                include_groups=True,
                limit=50,
            )
            # Prefer non-group, then any recent session with a spaces/ external id.
            session = next(
                (
                    s
                    for s in channel_sessions
                    if not s.is_group and str(s.external_conv_id or "").startswith("google_chat_spaces/")
                ),
                None,
            )
            if session is None:
                session = next(
                    (
                        s
                        for s in channel_sessions
                        if str(s.external_conv_id or "").startswith("google_chat_spaces/")
                    ),
                    None,
                )

        external = str(session.external_conv_id or "").strip() if session else ""
        if session is None or not external:
            return (
                f"❌ Google Chat proactive send to {member_name} requires them to message the bot first "
                "(DM preferred; group rooms work after any message in that space)"
            )

        try:
            space_name, thread_name = parse_external_conv_id(external)
        except ValueError:
            return f"❌ Google Chat session for {member_name} has an invalid space reference"

        await send_google_chat_message(
            config,
            space_name=space_name,
            text=message_text,
            thread_name=thread_name,
        )
        await _channel_provider_common()._save_channel_message(
            agent_tools,
            db=None,
            agent_id=agent_id,
            org_member=target_member,
            external_conv_id=external,
            source_channel="google_chat",
            message_text=message_text,
            log_label="GoogleChat",
        )
        return f"✅ Message sent to {member_name} via Google Chat"
    except Exception as error:
        logger.exception("[GoogleChat] Error")
        return f"❌ Google Chat message error: {str(error)[:200]}"

async def _send_wechat_channel_message(
    agent_id: uuid.UUID,
    member_name: str,
    message_text: str,
    target_member: Any,
) -> str:
    """Send proactive WeChat message using the latest cached context_token."""
    from app.services.wechat_channel import WECHAT_ILINK_BASE_URL, get_wechat_context_entry, send_wechat_text_message

    try:
        config = await channel_config_dao.get_configured_for_agent(agent_id, channel_type="wechat")
        if not config:
            return "❌ This agent has no WeChat channel configured"
        user_id = (target_member.external_id or "").strip()
        if not user_id:
            return f"❌ {member_name} has no WeChat user_id"

        context_entry = get_wechat_context_entry(config.extra_config, from_user_id=user_id)
        context_token = str((context_entry or {}).get("context_token") or "").strip()
        conv_id = str((context_entry or {}).get("conv_id") or f"wechat_{user_id}").strip()
        if not context_token:
            return f"❌ WeChat proactive send to {member_name} requires them to message the bot first"
        token = str((config.extra_config or {}).get("bot_token") or "").strip()
        base_url = str((config.extra_config or {}).get("baseurl") or WECHAT_ILINK_BASE_URL).strip()
        route_tag = str((config.extra_config or {}).get("route_tag") or "").strip() or None
        if not token:
            return "❌ WeChat bot token is missing"

        await send_wechat_text_message(
            token=token,
            base_url=base_url,
            to_user_id=user_id,
            context_token=context_token,
            text=message_text,
            route_tag=route_tag,
        )
        await _channel_provider_common()._save_channel_message(
            agent_tools,
            db=None,
            agent_id=agent_id,
            org_member=target_member,
            external_conv_id=conv_id,
            source_channel="wechat",
            message_text=message_text,
            log_label="WeChat",
        )
        return f"✅ Message sent to {member_name} via WeChat"
    except Exception as error:
        logger.exception("[WeChat] Error")
        return f"❌ WeChat message error: {str(error)[:200]}"
