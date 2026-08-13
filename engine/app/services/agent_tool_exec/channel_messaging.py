from __future__ import annotations

import importlib
import uuid
from types import ModuleType
from typing import Any

from app.core.logging import logger
from app.core.permissions import evaluate_human_relationship_status
from app.dao.agent_relationship_dao import agent_relationship_dao
from app.dao.user_dao import user_dao
from app.services import agent_tools

from .registry import ToolArguments


def _channel_providers() -> ModuleType:
    return importlib.import_module("app.services.agent_tool_exec.channel_providers")


def _normalize_provider_type(value: str | None) -> str | None:
    if not value:
        return None
    return "teams" if value == "microsoft_teams" else value


async def _send_channel_message(agent_id: uuid.UUID, args: ToolArguments) -> str:
    """Send a proactive channel message through the recipient's configured provider."""
    member_name = _string_argument(args, "member_name")
    message_text = _string_argument(args, "message")
    raw_target_channel = _string_argument(args, "channel").lower()
    target_channel = "teams" if raw_target_channel == "microsoft_teams" else raw_target_channel

    if not member_name:
        return "❌ Please provide member_name"
    if not message_text:
        return "❌ Please provide message content"

    try:
        relationships = await agent_relationship_dao.list_for_agent_with_members_and_providers(agent_id)
        rows: list[tuple[Any, Any, str | None]] = []
        for relationship in relationships:
            member = relationship.member
            if not member or member.name != member_name or member.status != "active":
                continue
            status_info = await evaluate_human_relationship_status(None, relationship)
            if status_info["access_status"] == "active":
                rows.append((relationship, member, _normalize_provider_type(relationship.provider_type)))

        if not rows:
            return f"❌ {member_name} is not in your relationship network"

        target_member: Any = None
        provider_type: str | None = None
        if target_channel:
            for _relationship, member, row_provider_type in rows:
                if row_provider_type == target_channel:
                    target_member = member
                    provider_type = row_provider_type
                    break
            if not target_member:
                available = sorted({channel for _, _, channel in rows if channel is not None})
                return (
                    f"❌ {member_name} not found in {target_channel} channel. "
                    f"Available channels: {', '.join(available)}"
                )
        else:
            if len(rows) > 1:
                available = [channel for _, _, channel in rows if channel]
                logger.warning(
                    f"[ChannelMessage] Ambiguous member '{member_name}' found in multiple channels: {available}"
                )
            _relationship, target_member, provider_type = rows[0]

        if not provider_type:
            if target_member.user_id:
                platform_user = await user_dao.get(target_member.user_id)
                if platform_user:
                    platform_identifier = platform_user.display_name or getattr(platform_user, "username", None)
                    if not platform_identifier and platform_user.identity is not None:
                        platform_identifier = getattr(platform_user.identity, "username", None)
                    platform_identifier = platform_identifier or member_name
                    logger.info(
                        "[ChannelMessage] %s is a platform user; rerouting send_channel_message -> send_platform_message",
                        member_name,
                    )
                    return await agent_tools._send_platform_message(
                        agent_id,
                        {"username": platform_identifier, "message": message_text},
                    )

            if target_member.external_id or target_member.open_id:
                provider_type = "feishu"
            else:
                return (
                    f"❌ {member_name} has no linked channel. "
                    "If they are a platform user, use send_platform_message instead."
                )

        logger.info(f"[ChannelMessage] Sending to {member_name} via {provider_type}")
        match provider_type:
            case "feishu":
                return await _channel_providers()._send_feishu_channel_message(
                    agent_id, member_name, message_text, target_member
                )
            case "dingtalk":
                return await _channel_providers()._send_dingtalk_message(
                    agent_id, member_name, message_text, target_member
                )
            case "wecom":
                return await _channel_providers()._send_wecom_message(
                    agent_id, member_name, message_text, target_member
                )
            case "slack":
                return await _channel_providers()._send_slack_message(
                    agent_id, member_name, message_text, target_member
                )
            case "teams":
                return await _channel_providers()._send_teams_channel_message(
                    agent_id, member_name, message_text, target_member
                )
            case "wechat":
                return await _channel_providers()._send_wechat_channel_message(
                    agent_id, member_name, message_text, target_member
                )
            case _:
                return f"❌ Unsupported channel type: {provider_type}"
    except Exception as error:
        logger.exception("[ChannelMessage] Error")
        return f"❌ Channel message error: {str(error)[:200]}"


def _string_argument(arguments: ToolArguments, name: str) -> str:
    value = arguments.get(name)
    return value.strip() if isinstance(value, str) else ""
