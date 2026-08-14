from __future__ import annotations

import importlib
import uuid
from collections.abc import Awaitable
from types import ModuleType
from typing import Protocol, TypeGuard

from app.records.org import OrgMemberRecord
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


class _ImProviders(Protocol):
    def _send_dingtalk_message(
        self,
        agent_id: uuid.UUID,
        member_name: str,
        message_text: str,
        target_member: OrgMemberRecord,
    ) -> Awaitable[str]: ...

    def _send_wecom_message(
        self,
        agent_id: uuid.UUID,
        member_name: str,
        message_text: str,
        target_member: OrgMemberRecord,
    ) -> Awaitable[str]: ...


class _ChatProviders(Protocol):
    def _send_slack_message(
        self,
        agent_id: uuid.UUID,
        member_name: str,
        message_text: str,
        target_member: OrgMemberRecord,
    ) -> Awaitable[str]: ...

    def _send_teams_channel_message(
        self,
        agent_id: uuid.UUID,
        member_name: str,
        message_text: str,
        target_member: OrgMemberRecord,
    ) -> Awaitable[str]: ...

    def _send_wechat_channel_message(
        self,
        agent_id: uuid.UUID,
        member_name: str,
        message_text: str,
        target_member: OrgMemberRecord,
    ) -> Awaitable[str]: ...

    def _send_google_chat_message(
        self,
        agent_id: uuid.UUID,
        member_name: str,
        message_text: str,
        target_member: OrgMemberRecord,
    ) -> Awaitable[str]: ...


def _is_im_providers(module: ModuleType) -> TypeGuard[_ImProviders]:
    return hasattr(module, "_send_dingtalk_message") and hasattr(module, "_send_wecom_message")


def _is_chat_providers(module: ModuleType) -> TypeGuard[_ChatProviders]:
    return all(
        hasattr(module, name)
        for name in (
            "_send_slack_message",
            "_send_teams_channel_message",
            "_send_wechat_channel_message",
            "_send_google_chat_message",
        )
    )


def _im_providers() -> _ImProviders:
    module = importlib.import_module("app.services.agent_tool_exec.channel_provider_im")
    if not _is_im_providers(module):
        raise ImportError("channel_provider_im is missing outbound senders")
    return module


def _chat_providers() -> _ChatProviders:
    module = importlib.import_module("app.services.agent_tool_exec.channel_provider_chat")
    if not _is_chat_providers(module):
        raise ImportError("channel_provider_chat is missing outbound senders")
    return module


async def _send_dingtalk_message(
    agent_id: uuid.UUID,
    member_name: str,
    message_text: str,
    target_member: OrgMemberRecord,
) -> str:
    return await _im_providers()._send_dingtalk_message(agent_id, member_name, message_text, target_member)


async def _send_wecom_message(
    agent_id: uuid.UUID,
    member_name: str,
    message_text: str,
    target_member: OrgMemberRecord,
) -> str:
    return await _im_providers()._send_wecom_message(agent_id, member_name, message_text, target_member)


async def _send_slack_message(
    agent_id: uuid.UUID,
    member_name: str,
    message_text: str,
    target_member: OrgMemberRecord,
) -> str:
    return await _chat_providers()._send_slack_message(agent_id, member_name, message_text, target_member)


async def _send_teams_channel_message(
    agent_id: uuid.UUID,
    member_name: str,
    message_text: str,
    target_member: OrgMemberRecord,
) -> str:
    return await _chat_providers()._send_teams_channel_message(agent_id, member_name, message_text, target_member)


async def _send_wechat_channel_message(
    agent_id: uuid.UUID,
    member_name: str,
    message_text: str,
    target_member: OrgMemberRecord,
) -> str:
    return await _chat_providers()._send_wechat_channel_message(agent_id, member_name, message_text, target_member)


async def _send_google_chat_message(
    agent_id: uuid.UUID,
    member_name: str,
    message_text: str,
    target_member: OrgMemberRecord,
) -> str:
    return await _chat_providers()._send_google_chat_message(agent_id, member_name, message_text, target_member)


async def _send_feishu_channel_message(
    agent_id: uuid.UUID,
    member_name: str,
    message_text: str,
    target_member: OrgMemberRecord,
) -> str:
    del target_member
    return await agent_tools._send_feishu_message(agent_id, {"member_name": member_name, "message": message_text})
