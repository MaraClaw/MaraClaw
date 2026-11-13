from __future__ import annotations

import json
from contextlib import asynccontextmanager, suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from app.api import dingtalk, google_chat, wecom
from app.core.domain.channels import ChannelType
from app.dao import agent_dao, channel_config_dao
from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.records.channel_config import ChannelConfigRecord
from app.records.chat import ChatSessionRecord
from app.services import dingtalk_stream, discord_gateway, openclaw_routing, wechat_message_processor, wecom_stream
from app.services.channel_user_service import ChannelUserResolutionError
from app.services.channels import binding, submissions
from app.services.channels.google_chat import parse_google_chat_event
from app.services.channels.media_limits import READ_CHUNK_BYTES, MediaBudget, too_large_note
from app.services.channels.stream_source import stream_source
from app.services.dingtalk_stream import DingTalkStreamManager
from app.services.dingtalk_token import DingTalkTokenManager
from app.services.document_parser import IMAGE_UPLOAD_MAX_BYTES
from app.services.storage_runtime.quota_errors import StorageQuotaError
from channel_media_fixtures import CountingStream, serve
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
    monkeypatch.setattr(submissions, "lock_channel_source", AsyncMock(return_value=config))
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
    monkeypatch.setattr(submissions, "charge_agent_daily_message", AsyncMock())
    monkeypatch.setattr(submissions, "prepare_shared_openclaw_message", prepare)
    monkeypatch.setattr(chat_session_dao, "lock_for_agent", locked)
    return agent, user, shared, route, config, history, persist, enqueue


async def invoke_processor(name, pipeline, *, group=False, native_id="native-id"):
    agent, _, _, _, config, *_ = pipeline
    config.channel_type = ChannelType(
        "wecom" if name.startswith("wecom") else "discord" if name == "discord_gateway" else name
    )
    config.extra_config = {
        **(config.extra_config or {}),
        "connection_mode": "gateway"
        if name == "discord_gateway"
        else "websocket"
        if name in {"dingtalk", "wecom_stream"}
        else "webhook",
    }
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
                native_event=submissions.ChannelEvent(
                    config_id=config.id,
                    agent_id=agent.id,
                    provider="dingtalk",
                    event_key=json.dumps([config.app_id, "chat-id", native_id], separators=(",", ":")),
                    sender_id="staff-id",
                    text="hello",
                    is_group=group,
                    conversation_id="dingtalk_group_chat-id" if group else "dingtalk_p2p_staff-id",
                    stream_source=stream_source(config),
                ),
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
                source=stream_source(config),
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
            await discord_gateway.DiscordGatewayManager()._handle_message(
                agent.id, message, "hello", source=stream_source(config)
            )
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


@pytest.fixture
def dingtalk_media_download(pipeline, monkeypatch):
    config = pipeline[4]
    config.channel_type = "dingtalk"
    config.app_id, config.app_secret = "app-key", "app-secret"
    store = AsyncMock(return_value=("key", "workspace/uploads/capture.png", "path"))
    stream = CountingStream(total=3 * IMAGE_UPLOAD_MAX_BYTES)
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"downloadUrl": "https://files.invalid/capture.png"})
        return httpx.Response(200, stream=stream)

    serve(monkeypatch, handler)
    monkeypatch.setattr(dingtalk_stream.dingtalk_token_manager, "get_token", AsyncMock(return_value="token"))
    monkeypatch.setattr(dingtalk_stream, "store_agent_upload", store)
    return store, stream, requests


FILE_MESSAGE: dingtalk_stream._DingTalkMessage = {
    "msgtype": "file",
    "content": {"downloadCode": "code", "fileName": "capture.png"},
}


async def deliver_dingtalk_file(agent_id, msg_data: dingtalk_stream._DingTalkMessage = FILE_MESSAGE):
    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type=ChannelType.DINGTALK)
    assert config is not None
    await DingTalkStreamManager._handle_media_and_dispatch(
        msg_data=msg_data,
        app_key="app-key",
        app_secret="app-secret",
        agent_id=agent_id,
        sender_staff_id="staff-id",
        conversation_id="chat-id",
        conversation_type="1",
        session_webhook="https://provider.invalid/reply",
        message_id="native-id",
        source=stream_source(config),
    )


async def test_oversize_dingtalk_file_is_skipped_with_a_note_and_the_event_is_accepted(
    pipeline, dingtalk_media_download
):
    # Given: a DingTalk file named like an image whose body is far past the 10 MiB image cap, served without length
    agent, *_, persist, enqueue = pipeline
    store, stream, _ = dingtalk_media_download
    # When
    await deliver_dingtalk_file(agent.id)
    # Then: the read stopped at the cap, nothing was stored, and the transcript carries the note
    note = too_large_note("capture.png", IMAGE_UPLOAD_MAX_BYTES)
    assert IMAGE_UPLOAD_MAX_BYTES < stream.consumed <= IMAGE_UPLOAD_MAX_BYTES + READ_CHUNK_BYTES
    store.assert_not_awaited()
    assert persist.await_args.kwargs["content"] == note
    assert enqueue.await_args.args[0].content == note


async def test_redelivered_oversize_dingtalk_event_replays_without_a_second_download(pipeline, dingtalk_media_download):
    # Given: an oversize event that was accepted once
    agent, *_, persist, _ = pipeline
    store, _, requests = dingtalk_media_download
    await deliver_dingtalk_file(agent.id)
    downloads = len(requests)
    receipts = submissions.channel_event_receipt_dao
    assert isinstance(receipts.find, AsyncMock)
    assert isinstance(receipts.reserve, AsyncMock)
    receipts.find.return_value = receipts.reserve.await_args_list[0].args[0]
    # When: the provider delivers the same event again
    await deliver_dingtalk_file(agent.id)
    # Then: the receipt answers it, with no new request, storage write or transcript row
    assert len(requests) == downloads
    receipts.reserve.assert_awaited_once()
    persist.assert_awaited_once()
    store.assert_not_awaited()


async def test_unauthorized_dingtalk_event_is_refused_before_any_download(
    pipeline, dingtalk_media_download, monkeypatch
):
    # Given: a sender who no longer has access to the agent
    agent, *_, persist, _ = pipeline
    store, _, requests = dingtalk_media_download
    monkeypatch.setattr(binding, "check_agent_access", AsyncMock(side_effect=HTTPException(403)))
    # When / Then: authorization fails before the provider is contacted
    with pytest.raises(HTTPException):
        await deliver_dingtalk_file(agent.id)
    assert requests == []
    store.assert_not_awaited()
    persist.assert_not_awaited()


@pytest.mark.parametrize(
    "failure", ["token_503", "token_timeout", "lookup_503", "download_503", "read_timeout", "storage_quota"]
)
async def test_dingtalk_transient_failures_propagate_without_accepting(
    pipeline, dingtalk_media_download, monkeypatch, failure
):
    # Given: a failure at the download-URL lookup, at the file download, or in storage
    agent, *_, persist, _ = pipeline
    store, _, _ = dingtalk_media_download
    if failure.startswith("token_"):
        monkeypatch.setattr(dingtalk_stream, "dingtalk_token_manager", DingTalkTokenManager())
        monkeypatch.setattr("app.services.im_token_cache.get_cached_im_token", AsyncMock(return_value=None))

    def handler(request):
        if request.url.path == "/v1.0/oauth2/accessToken":
            if failure == "token_timeout":
                raise httpx.ReadTimeout("slow token provider", request=request)
            return httpx.Response(503)
        if request.method == "POST":
            if failure == "lookup_503":
                return httpx.Response(503)
            return httpx.Response(200, json={"downloadUrl": "https://files.invalid/capture.png"})
        if failure == "read_timeout":
            raise httpx.ReadTimeout("slow provider", request=request)
        return httpx.Response(503 if failure == "download_503" else 200, content=b"file-bytes")

    serve(monkeypatch, handler)
    store.side_effect = StorageQuotaError("agent storage is full")
    expected = {
        "token_timeout": httpx.ReadTimeout,
        "read_timeout": httpx.ReadTimeout,
        "storage_quota": StorageQuotaError,
    }.get(failure, httpx.HTTPStatusError)
    # When / Then: the stream acks with a system exception so DingTalk redelivers, and no receipt is reserved
    with pytest.raises(expected):
        await deliver_dingtalk_file(agent.id)
    assert isinstance(submissions.channel_event_receipt_dao.reserve, AsyncMock)
    submissions.channel_event_receipt_dao.reserve.assert_not_awaited()
    persist.assert_not_awaited()


async def test_dingtalk_file_with_an_expired_download_url_is_still_accepted_without_the_file(
    pipeline, dingtalk_media_download, monkeypatch
):
    # Given: DingTalk refuses the download with 403
    agent, *_, persist, _ = pipeline
    store, _, _ = dingtalk_media_download
    serve(
        monkeypatch,
        lambda request: (
            httpx.Response(200, json={"downloadUrl": "https://files.invalid/capture.png"})
            if request.method == "POST"
            else httpx.Response(403)
        ),
    )
    # When
    await deliver_dingtalk_file(agent.id)
    # Then: the event is accepted and nothing was stored
    persist.assert_awaited_once()
    store.assert_not_awaited()


@pytest.mark.parametrize("status", [200, 403])
async def test_dingtalk_media_token_refusal_keeps_the_non_transient_fallback(
    pipeline, dingtalk_media_download, monkeypatch, status
):
    agent, *_, persist, _ = pipeline
    store, _, _ = dingtalk_media_download
    monkeypatch.setattr(dingtalk_stream, "dingtalk_token_manager", DingTalkTokenManager())
    monkeypatch.setattr("app.services.im_token_cache.get_cached_im_token", AsyncMock(return_value=None))
    serve(monkeypatch, lambda _: httpx.Response(status, text="invalid token response"))
    await deliver_dingtalk_file(agent.id)
    persist.assert_awaited_once()
    store.assert_not_awaited()


async def test_aborted_oversize_dingtalk_read_spends_the_event_so_the_next_image_is_never_requested(
    pipeline, dingtalk_media_download, monkeypatch
):
    # Given: an event allowance of three read chunks, and a rich text message with two images
    agent, *_, persist, _ = pipeline
    store, _, _ = dingtalk_media_download
    monkeypatch.setattr(dingtalk_stream, "MediaBudget", lambda: MediaBudget(remaining=3 * READ_CHUNK_BYTES))
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "POST":
            code = json.loads(request.content)["downloadCode"]
            return httpx.Response(200, json={"downloadUrl": f"https://files.invalid/{code}"})
        return httpx.Response(200, stream=CountingStream(total=10 * READ_CHUNK_BYTES))

    serve(monkeypatch, handler)
    message: dingtalk_stream._DingTalkMessage = {
        "msgtype": "richText",
        "content": {"richText": [{"downloadCode": "code-a"}, {"downloadCode": "code-b"}]},
    }
    # When
    await deliver_dingtalk_file(agent.id, message)
    # Then: the second image cost no lookup and no download, and both are noted as skipped
    assert [(request.method, request.url.path) for request in requests] == [
        ("POST", "/v1.0/robot/messageFiles/download"),
        ("GET", "/code-a"),
    ]
    store.assert_not_awaited()
    assert persist.await_args.kwargs["content"].count("[file skipped:") == 2
