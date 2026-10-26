from __future__ import annotations

from contextlib import asynccontextmanager, suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api import dingtalk, google_chat, wecom
from app.dao import agent_dao, channel_config_dao
from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.records.channel_config import ChannelConfigRecord
from app.records.chat import ChatSessionRecord
from app.services import discord_gateway, openclaw_routing, wechat_message_processor, wecom_stream
from app.services.channel_user_service import ChannelUserResolutionError
from app.services.channels import binding, submissions
from app.services.channels.google_chat import parse_google_chat_event
from test_channel_shared_binding import context

__all__ = ["context"]

PROCESSORS = ["dingtalk", "wecom", "wecom_kf", "wecom_stream", "wechat", "discord_gateway", "google_chat"]
GROUP_PROCESSORS = ["dingtalk", "wecom", "wecom_stream", "discord_gateway", "google_chat"]


@pytest.fixture
def pipeline(context, monkeypatch):
    agent, user, shared = context
    route = ChatSessionRecord(
        id=uuid4(), agent_id=agent.id, user_id=user.id, source_channel="dingtalk", external_conv_id="provider-route"
    )
    monkeypatch.setattr(submissions, "find_or_create_channel_session", AsyncMock(return_value=route))
    monkeypatch.setattr(agent_dao, "get", AsyncMock(return_value=agent))
    monkeypatch.setattr("app.services.wechat_channel.remember_wechat_context", AsyncMock())
    history = AsyncMock(return_value=[])
    persist, enqueue = AsyncMock(), AsyncMock()
    monkeypatch.setattr(chat_message_dao, "list_recent", history)
    monkeypatch.setattr(submissions, "persist_channel_user_message", persist)
    monkeypatch.setattr(submissions, "enqueue_prepared_openclaw_message", enqueue)
    config = ChannelConfigRecord(id=uuid4(), agent_id=agent.id, is_configured=True, extra_config={"bot_token": "token"})
    monkeypatch.setattr(channel_config_dao, "get", AsyncMock(return_value=config))
    monkeypatch.setattr(channel_config_dao, "get_for_agent", AsyncMock(return_value=config))
    monkeypatch.setattr(submissions, "load_user_principal", AsyncMock(return_value=user))

    async def access(principal, target):
        _, level = await binding.check_agent_access(principal, target.id, fresh=True)
        return level

    @asynccontextmanager
    async def connection():
        yield

    async def locked(session_id, _agent_id):
        return shared if session_id == shared.id else route

    async def prepare(*, agent, content, history):
        return openclaw_routing.PreparedOpenClawMessage(agent, content, "primary", None, None, "channel-unit")

    monkeypatch.setattr(submissions, "compute_agent_access_level", access)
    monkeypatch.setattr(submissions, "connection_ctx", connection)
    monkeypatch.setattr(submissions.channel_event_receipt_dao, "reserve", AsyncMock(return_value=True))
    monkeypatch.setattr(submissions.channel_event_receipt_dao, "find", AsyncMock(return_value=None))
    monkeypatch.setattr(submissions.channel_event_receipt_dao, "lock_conversation", AsyncMock())
    monkeypatch.setattr(submissions, "load_agent_for_new_work", AsyncMock(return_value=agent))
    monkeypatch.setattr(submissions, "charge_conversation_quota", AsyncMock())
    monkeypatch.setattr(submissions, "prepare_openclaw_message", prepare)
    monkeypatch.setattr(chat_session_dao, "lock_for_agent", locked)
    return agent, user, shared, route, config, history, persist, enqueue


async def invoke_processor(name, pipeline, *, group=False, native_id="native-id"):
    agent, _, _, _, config, *_ = pipeline
    config.channel_type = "wecom" if name.startswith("wecom") else "discord" if name == "discord_gateway" else name
    match name:
        case "dingtalk":
            await dingtalk.process_dingtalk_message(
                agent.id,
                "staff-id",
                "hello",
                "chat-id",
                "2" if group else "1",
                "https://provider.invalid/reply",
                image_base64_list=["data:image/jpeg;base64,aGVsbG8="],
                saved_file_paths=["workspace/uploads/photo.jpg"],
                message_id=native_id,
            )
        case "wecom" | "wecom_kf":
            await wecom._process_wecom_text(
                agent.id,
                config,
                "external-id",
                "hello",
                is_kf=name == "wecom_kf",
                chat_id="chat-id" if group else "",
                message_id=native_id,
                kf_msg_id=native_id if name == "wecom_kf" else None,
            )
        case "wecom_stream":
            await wecom_stream._process_wecom_stream_message(
                agent.id,
                "external-id",
                "hello",
                chat_id="chat-id" if group else "",
                chat_type="group" if group else "single",
                message_id=native_id,
            )
        case "wechat":
            await wechat_message_processor.process_wechat_message(
                agent.id,
                {
                    "message_id": native_id,
                    "from_user_id": "external-id",
                    "context_token": "context",
                    "session_id": "external-session",
                    "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
                },
                config,
            )
        case "discord_gateway":
            message = MagicMock(spec=discord_gateway._DiscordMessageLike)
            message.author = SimpleNamespace(id=123, display_name="Human", name="Human")
            message.channel = SimpleNamespace(id=456)
            message.guild = SimpleNamespace(id=789) if group else None
            if native_id:
                message.id = native_id
            await discord_gateway.DiscordGatewayManager()._handle_message(agent.id, message, "hello")
        case "google_chat":
            event = parse_google_chat_event(
                {
                    "type": "MESSAGE",
                    "message": {
                        "name": native_id,
                        "text": "hello",
                        "sender": {"name": "users/123", "type": "HUMAN"},
                        "space": {"name": "spaces/ABC", "type": "ROOM" if group else "DM"},
                    },
                }
            )
            assert event is not None
            await google_chat._process_message_event(agent_id=agent.id, config=config, event=event)
        case _:
            raise AssertionError(name)


@pytest.mark.parametrize("processor", PROCESSORS)
async def test_direct_processor_uses_shared_history_and_original_reply_address(pipeline, processor):
    # Given
    _, _, shared, route, _, history, persist, enqueue = pipeline
    # When
    await invoke_processor(processor, pipeline)
    # Then
    assert history.await_args.kwargs["conversation_id"] == str(shared.id)
    assert persist.await_args.kwargs["conversation_id"] == str(shared.id)
    assert enqueue.await_args.kwargs["conversation_id"] == str(shared.id)
    assert enqueue.await_args.kwargs["reply_session_id"] == route.id
    if processor == "dingtalk":
        assert persist.await_args.kwargs["content"] == "[file:workspace/uploads/photo.jpg]\nhello"
        assert "[image_data:" in enqueue.await_args.args[0].content


@pytest.mark.parametrize("processor", GROUP_PROCESSORS)
async def test_group_processor_preserves_legacy_provider_history(pipeline, processor):
    # Given
    _, _, _, route, _, history, persist, enqueue = pipeline
    assert not route.is_group
    # When
    await invoke_processor(processor, pipeline, group=True)
    # Then
    assert history.await_args.kwargs["conversation_id"] == str(route.id)
    assert persist.await_args.kwargs["conversation_id"] == str(route.id)
    assert enqueue.await_args.kwargs["conversation_id"] == str(route.id)
    assert isinstance(binding.ensure_shared_session, AsyncMock)
    binding.ensure_shared_session.assert_not_awaited()


@pytest.mark.parametrize("processor", PROCESSORS)
@pytest.mark.parametrize("rejection", ["unbound", "unauthorized"])
async def test_rejected_processor_never_reads_or_writes_shared_context(pipeline, monkeypatch, processor, rejection):
    # Given
    *_, history, persist, enqueue = pipeline
    if rejection == "unbound":
        monkeypatch.setattr(binding.channel_user_service, "_find_org_member", AsyncMock(return_value=None))
    else:
        monkeypatch.setattr(binding, "check_agent_access", AsyncMock(side_effect=HTTPException(403)))
    # When
    with suppress(ChannelUserResolutionError, HTTPException):
        await invoke_processor(processor, pipeline)
    # Then
    history.assert_not_awaited()
    persist.assert_not_awaited()
    enqueue.assert_not_awaited()
