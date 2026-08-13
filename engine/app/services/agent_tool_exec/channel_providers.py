from __future__ import annotations

import importlib
import uuid
from types import ModuleType
from typing import Any

from app.services import agent_tools

__all__ = (
    "_send_dingtalk_message",
    "_send_feishu_channel_message",
    "_send_google_chat_message",
    "_send_slack_message",
    "_send_teams_channel_message",
    "_send_wechat_channel_message",
    "_send_wecom_message",
)


def _im_providers() -> ModuleType:
    return importlib.import_module("app.services.agent_tool_exec.channel_provider_im")


def _chat_providers() -> ModuleType:
    return importlib.import_module("app.services.agent_tool_exec.channel_provider_chat")


async def _send_dingtalk_message(
    agent_id: uuid.UUID,
    member_name: str,
    message_text: str,
    target_member: Any,
) -> str:
    return await _im_providers()._send_dingtalk_message(agent_id, member_name, message_text, target_member)


async def _send_wecom_message(
    agent_id: uuid.UUID,
    member_name: str,
    message_text: str,
    target_member: Any,
) -> str:
    return await _im_providers()._send_wecom_message(agent_id, member_name, message_text, target_member)


async def _send_slack_message(
    agent_id: uuid.UUID,
    member_name: str,
    message_text: str,
    target_member: Any,
) -> str:
    return await _chat_providers()._send_slack_message(agent_id, member_name, message_text, target_member)


async def _send_teams_channel_message(
    agent_id: uuid.UUID,
    member_name: str,
    message_text: str,
    target_member: Any,
) -> str:
    return await _chat_providers()._send_teams_channel_message(agent_id, member_name, message_text, target_member)


async def _send_wechat_channel_message(
    agent_id: uuid.UUID,
    member_name: str,
    message_text: str,
    target_member: Any,
) -> str:
    return await _chat_providers()._send_wechat_channel_message(agent_id, member_name, message_text, target_member)


async def _send_google_chat_message(
    agent_id: uuid.UUID,
    member_name: str,
    message_text: str,
    target_member: Any,
) -> str:
    return await _chat_providers()._send_google_chat_message(agent_id, member_name, message_text, target_member)


async def _send_feishu_channel_message(
    agent_id: uuid.UUID,
    member_name: str,
    message_text: str,
    target_member: Any,
) -> str:
    del target_member
    return await agent_tools._send_feishu_message(agent_id, {"member_name": member_name, "message": message_text})
