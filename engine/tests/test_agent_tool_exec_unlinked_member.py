"""Proactive channel sends to an organization member who has no linked platform user."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.records.org import OrgMemberRecord
from app.services import dingtalk_service
from app.services.agent_tool_exec import channel_provider_chat, channel_provider_common, channel_provider_im
from app.services.channels import google_chat
from im_token_cache_fakes import captured_app_log


def _unlinked_member() -> OrgMemberRecord:
    return OrgMemberRecord(id=uuid.uuid4(), name="Alex", external_id="alex-1", tenant_id=uuid.uuid4())


async def test_dingtalk_send_to_unlinked_member_reports_success_and_saves_no_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a delivered DingTalk message to a member with no linked platform user.
    member = _unlinked_member()
    config = SimpleNamespace(app_id="app", app_secret="secret", extra_config={})
    monkeypatch.setattr(
        channel_provider_im.channel_config_dao, "get_configured_for_agent", AsyncMock(return_value=config)
    )
    monkeypatch.setattr(dingtalk_service, "send_dingtalk_message", AsyncMock(return_value={"errcode": 0}))
    monkeypatch.setattr(channel_provider_common.agent_dao, "get", AsyncMock(return_value=None))
    monkeypatch.setattr(channel_provider_common, "linked_platform_user", AsyncMock(return_value=None))
    find_session = AsyncMock()
    insert_message = AsyncMock()
    monkeypatch.setattr(channel_provider_common, "find_or_create_channel_session", find_session)
    monkeypatch.setattr(channel_provider_common.chat_message_dao, "insert_message", insert_message)

    # When the proactive send persists its history.
    with captured_app_log() as log:
        result = await channel_provider_im._send_dingtalk_message(uuid.uuid4(), "Alex", "private text", member)

    # Then the agent still sees the send succeed, nothing is saved, and the log names the member but not the message.
    assert result == "✅ Message sent to Alex via DingTalk"
    find_session.assert_not_awaited()
    insert_message.assert_not_awaited()
    assert any(str(member.id) in line for line in log)
    assert not any("private text" in line for line in log)


async def test_teams_proactive_send_to_unlinked_member_asks_them_to_message_the_bot_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member = _unlinked_member()
    config = SimpleNamespace(extra_config={"service_url": "https://smba.example.test"})
    monkeypatch.setattr(
        channel_provider_chat.channel_config_dao, "get_configured_for_agent", AsyncMock(return_value=config)
    )
    monkeypatch.setattr(channel_provider_chat.agent_dao, "get", AsyncMock(return_value=None))
    monkeypatch.setattr(channel_provider_chat, "linked_platform_user", AsyncMock(return_value=None))
    list_sessions = AsyncMock(return_value=[])
    send = AsyncMock()
    monkeypatch.setattr(channel_provider_chat.chat_session_dao, "list_for_user", list_sessions)
    monkeypatch.setattr("app.api.teams._send_teams_message", send)

    result = await channel_provider_chat._send_teams_channel_message(uuid.uuid4(), "Alex", "hello", member)

    assert result == "❌ Teams proactive send to Alex requires them to message the bot first"
    list_sessions.assert_not_awaited()
    send.assert_not_awaited()


async def test_google_chat_proactive_send_to_unlinked_member_never_uses_another_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an agent that has a Google Chat space of its own and a member who never could have written to it.
    member = _unlinked_member()
    space = SimpleNamespace(is_group=False, external_conv_id="google_chat_spaces/AAA")
    monkeypatch.setattr(
        channel_provider_chat.channel_config_dao, "get_configured_for_agent", AsyncMock(return_value=object())
    )
    monkeypatch.setattr(channel_provider_chat.agent_dao, "get", AsyncMock(return_value=None))
    monkeypatch.setattr(channel_provider_chat, "linked_platform_user", AsyncMock(return_value=None))
    list_member_sessions = AsyncMock(return_value=[])
    list_agent_sessions = AsyncMock(return_value=[space])
    send = AsyncMock()
    monkeypatch.setattr(channel_provider_chat.chat_session_dao, "list_for_user", list_member_sessions)
    monkeypatch.setattr(channel_provider_chat.chat_session_dao, "list_for_agent_channel", list_agent_sessions)
    monkeypatch.setattr(google_chat, "send_google_chat_message", send)

    # When the agent sends the member a proactive message.
    result = await channel_provider_chat._send_google_chat_message(uuid.uuid4(), "Alex", "hello", member)

    # Then the send is refused instead of landing in that other conversation.
    assert result.startswith("❌ Google Chat proactive send to Alex requires them to message the bot first")
    list_member_sessions.assert_not_awaited()
    list_agent_sessions.assert_not_awaited()
    send.assert_not_awaited()
