from __future__ import annotations

import importlib
import uuid
from types import ModuleType

from app.core.logging import logger
from app.dao.channel_config_dao import channel_config_dao
from app.records.org import OrgMemberRecord
from app.services import agent_tools


def _channel_provider_common() -> ModuleType:
    return importlib.import_module("app.services.agent_tool_exec.channel_provider_common")


async def _send_dingtalk_message(
    agent_id: uuid.UUID,
    member_name: str,
    message_text: str,
    target_member: OrgMemberRecord,
) -> str:
    """Send message via DingTalk channel using Open API."""
    from app.services.dingtalk_service import send_dingtalk_message

    try:
        config = await channel_config_dao.get_configured_for_agent(agent_id, channel_type="dingtalk")
        if not config:
            return "❌ This agent has no DingTalk channel configured"

        user_id = target_member.external_id
        if not user_id:
            user_id = target_member.unionid or target_member.open_id
            if not user_id:
                return f"❌ {member_name} has no DingTalk user_id"

        logger.info(f"[DingTalk] Sending to user_id: {user_id}")
        app_id = config.app_id
        app_secret = config.app_secret
        if not app_id or not app_secret:
            return "❌ This agent has no DingTalk channel configured"
        dingtalk_agent_id = (config.extra_config or {}).get("agent_id")
        result = await send_dingtalk_message(
            app_id=app_id,
            app_secret=app_secret,
            user_id=user_id,
            message=message_text,
            agent_id=dingtalk_agent_id or "",
        )

        if result.get("errcode") == 0:
            try:
                await _channel_provider_common()._save_channel_message(
                    agent_tools,
                    db=None,
                    agent_id=agent_id,
                    org_member=target_member,
                    external_conv_id=f"dingtalk_p2p_{user_id}",
                    source_channel="dingtalk",
                    message_text=message_text,
                    log_label="DingTalk",
                )
            except Exception as error:
                logger.error(f"[DingTalk] Failed to save proactive message to session: {error}")
            return f"✅ Message sent to {member_name} via DingTalk"
        errmsg = result.get("errmsg", "Unknown error")
        logger.error(f"[DingTalk] Send failed: {result}")
        return f"❌ DingTalk send failed: {errmsg}"
    except Exception as error:
        logger.exception("[DingTalk] Error")
        return f"❌ DingTalk message error: {str(error)[:200]}"


async def _send_wecom_message(
    agent_id: uuid.UUID,
    member_name: str,
    message_text: str,
    target_member: OrgMemberRecord,
) -> str:
    """Send message via WeCom channel using Open API."""
    from app.services.wecom_service import send_wecom_message

    try:
        config = await channel_config_dao.get_configured_for_agent(agent_id, channel_type="wecom")
        if not config:
            return "❌ This agent has no WeCom channel configured"

        user_id = target_member.external_id
        if not user_id:
            user_id = target_member.open_id
            if not user_id:
                return f"❌ {member_name} has no WeCom user_id"

        logger.info(f"[WeCom] Sending to user_id: {user_id}")
        app_id = config.app_id
        app_secret = config.app_secret
        if not app_id or not app_secret:
            return "❌ This agent has no WeCom channel configured"
        result = await send_wecom_message(app_id, app_secret, user_id, message_text)

        if result.get("errcode") == 0:
            try:
                await _channel_provider_common()._save_channel_message(
                    agent_tools,
                    db=None,
                    agent_id=agent_id,
                    org_member=target_member,
                    external_conv_id=f"wecom_p2p_{user_id}",
                    source_channel="wecom",
                    message_text=message_text,
                    log_label="WeCom",
                )
            except Exception as error:
                logger.error(f"[WeCom] Failed to save proactive message to session: {error}")
            return f"✅ Message sent to {member_name} via WeCom"
        errmsg = result.get("errmsg", "Unknown error")
        logger.error(f"[WeCom] Send failed: {result}")
        return f"❌ WeCom send failed: {errmsg}"
    except Exception as error:
        logger.exception("[WeCom] Error")
        return f"❌ WeCom message error: {str(error)[:200]}"
