from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from tests.channel_shared_webhook_fixtures import (
    WEBHOOKS,
    context,
    invoke_webhook,
    pipeline,
    webhook_pipeline,
)
from tests.test_channel_shared_processors import PROCESSORS, invoke_processor

from app.api import dingtalk
from app.core.domain.channels import ChannelType
from app.core.json_types import JsonObject
from app.dao.channel_config_dao import channel_config_dao
from app.services.channels import feishu_submissions, slack_submissions, submissions
from app.services.channels.stream_source import stream_source
from app.services.channels.submissions import AcceptedChannelEvent, ChannelEvent

__all__ = ["context", "pipeline", "webhook_pipeline"]


@pytest.fixture
def native_events(monkeypatch):
    captured: list[ChannelEvent] = []

    async def accept(event, **_kwargs):
        captured.append(event)
        return AcceptedChannelEvent(replayed=True)

    async def replay(event):
        captured.append(event)
        return True

    monkeypatch.setattr(submissions, "accept_channel_event", accept)
    monkeypatch.setattr(dingtalk, "accept_channel_event", accept)
    for module in (submissions, feishu_submissions, slack_submissions):
        monkeypatch.setattr(module, "is_channel_event_replay", replay)
    return captured


@pytest.mark.parametrize("webhook", WEBHOOKS)
async def test_webhook_passes_native_identity_before_legacy_pipeline(
    webhook_pipeline, native_events, monkeypatch, webhook
):
    # Given a real provider-native identity and a replay accepted by the durable owner.
    *_, history, persist, enqueue = webhook_pipeline[0]
    # When the authenticated adapter receives the event.
    await invoke_webhook(webhook, webhook_pipeline, monkeypatch, native_id="native-event-123")
    # Then no legacy history, transcript, or content-deduplicating enqueue path ran.
    assert len(native_events) == 1
    assert "native-event-123" in native_events[0].event_key
    history.assert_not_awaited()
    persist.assert_not_awaited()
    enqueue.assert_not_awaited()


@pytest.mark.parametrize("processor", PROCESSORS)
async def test_stream_processor_passes_native_identity_before_legacy_pipeline(
    pipeline, native_events, monkeypatch, processor
):
    # Given a native stream message, including providers with account/container-scoped IDs.
    config = pipeline[4]
    monkeypatch.setattr(dingtalk.channel_config_dao, "get_for_agent", AsyncMock(return_value=config))
    *_, history, persist, enqueue = pipeline
    # When the connector processes it.
    await invoke_processor(processor, pipeline, native_id="native-event-123")
    # Then acceptance receives the native ID before legacy model/history preparation.
    assert len(native_events) == 1
    assert "native-event-123" in native_events[0].event_key
    if processor in {"dingtalk", "wecom_stream", "discord_gateway"}:
        assert native_events[0].stream_source == stream_source(config)
    history.assert_not_awaited()
    persist.assert_not_awaited()
    enqueue.assert_not_awaited()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("text", "different"),
        ("sender_id", "other"),
        ("conversation_id", "other"),
        ("kind", "image"),
        ("media_ids", ("other-native-image",)),
        ("is_group", True),
    ],
)
def test_fingerprint_changes_only_with_native_semantic_payload(field, value):
    # Given one immutable native semantic payload.
    event = ChannelEvent(uuid4(), uuid4(), "slack", "native-1", "sender", "conversation", "hello")
    # When one native semantic field changes.
    modified = replace(event, **{field: value})
    # Then receipt identity reuse is detectable independently of model/display/local-path enrichment.
    assert modified.fingerprint() != event.fingerprint()


@pytest.mark.parametrize("provider", ["feishu", "dingtalk", "wecom", "discord"])
@pytest.mark.parametrize("operation", ["replay", "accept"])
@pytest.mark.parametrize("revocation", ["secret", "mode", "disabled", "deleted", "recreated", "missing"])
async def test_stream_revocation_precedes_even_matching_receipt_reads(
    pipeline, monkeypatch, provider, operation, revocation
):
    config = pipeline[4]
    config.channel_type = ChannelType(provider)
    config.app_id, config.app_secret = "account", "secret"
    config.extra_config = {"connection_mode": "gateway" if provider == "discord" else "websocket"}
    source = stream_source(config)
    event = ChannelEvent(
        config.id, config.agent_id, provider, "native-1", "sender", "conversation", "hello", stream_source=source
    )
    assert event.fingerprint() == replace(event, stream_source=None).fingerprint()
    receipts = submissions.channel_event_receipt_dao
    find = AsyncMock(side_effect=AssertionError("revoked source reached receipt lookup"))
    reserve = AsyncMock(side_effect=AssertionError("revoked source reached reservation"))
    monkeypatch.setattr(receipts, "find", find)
    monkeypatch.setattr(receipts, "reserve", reserve)
    match revocation:
        case "secret":
            if provider == "wecom":
                config.extra_config = {**config.extra_config, "bot_secret": "rotated"}
            else:
                config.app_secret = "rotated"
        case "mode":
            config.extra_config = {"connection_mode": "webhook"}
        case "disabled":
            config.is_configured = False
        case "deleted":
            monkeypatch.setattr(submissions, "lock_channel_source", AsyncMock(return_value=None))
            monkeypatch.setattr(channel_config_dao, "get", AsyncMock(return_value=None))
        case "recreated":
            config.id = uuid4()
        case "missing":
            event = replace(event, stream_source=None)
    operation_fn = submissions.is_channel_event_replay if operation == "replay" else submissions.accept_channel_event
    with pytest.raises(HTTPException) as refused:
        await operation_fn(event)
    assert refused.value.status_code == 403
    find.assert_not_awaited()
    reserve.assert_not_awaited()
    pipeline[-2].assert_not_awaited()
    pipeline[-1].assert_not_awaited()


async def test_feishu_rotation_during_resource_download_forbids_storage_and_admission(pipeline, monkeypatch):
    config = pipeline[4]
    config.channel_type = ChannelType.FEISHU
    config.app_id, config.app_secret = "app", "secret"
    config.extra_config = {"connection_mode": "websocket"}
    source = stream_source(config)
    replay, store, accept = AsyncMock(return_value=False), AsyncMock(), AsyncMock()

    async def download(*args, **kwargs):
        config.app_secret = "rotated"
        return b"image"

    monkeypatch.setattr(feishu_submissions, "is_channel_event_replay", replay)
    monkeypatch.setattr(feishu_submissions.feishu_service, "download_message_resource", download)
    monkeypatch.setattr(feishu_submissions, "store_agent_upload", store)
    monkeypatch.setattr(feishu_submissions, "accept_channel_event", accept)
    event: JsonObject = {
        "sender": {"sender_id": {"user_id": "sender"}},
        "message": {"message_id": "native-1", "message_type": "image", "content": '{"image_key":"image"}'},
    }
    with pytest.raises(HTTPException) as refused:
        await feishu_submissions.accept_feishu_event(config, event, source=source)
    assert refused.value.status_code == 403
    replay.assert_awaited_once()
    store.assert_not_awaited()
    accept.assert_not_awaited()


@pytest.mark.parametrize("stage", ["token", "lookup", "download"])
async def test_dingtalk_rotation_between_external_steps_forbids_later_effects(pipeline, monkeypatch, stage):
    from app.services import dingtalk_stream

    config = pipeline[4]
    config.channel_type = ChannelType.DINGTALK
    config.app_id, config.app_secret = "app", "secret"
    source = stream_source(config)
    calls = []

    async def external(name, result):
        calls.append(name)
        if stage == name:
            config.app_secret = "rotated"
        return result

    async def token(*args):
        return await external("token", "token")

    async def lookup(*args):
        return await external("lookup", "https://files.invalid/image")

    async def download(*args):
        return await external("download", b"image")

    store, accept = AsyncMock(), AsyncMock()
    monkeypatch.setattr(dingtalk_stream, "is_channel_event_replay", AsyncMock(return_value=False))
    monkeypatch.setattr(dingtalk_stream.dingtalk_token_manager, "get_token", token)
    monkeypatch.setattr(dingtalk_stream, "_get_media_download_url", lookup)
    monkeypatch.setattr(dingtalk_stream, "_download_file", download)
    monkeypatch.setattr(dingtalk_stream, "store_agent_upload", store)
    monkeypatch.setattr(dingtalk, "process_dingtalk_message", accept)
    with pytest.raises(HTTPException) as refused:
        await dingtalk_stream.DingTalkStreamManager._handle_media_and_dispatch(
            {"msgtype": "picture", "content": {"downloadCode": "code"}},
            "app",
            "secret",
            config.agent_id,
            "sender",
            "conversation",
            "1",
            "",
            message_id="native-1",
            source=source,
        )
    assert refused.value.status_code == 403
    assert calls == ["token", "lookup", "download"][: ["token", "lookup", "download"].index(stage) + 1]
    store.assert_not_awaited()
    accept.assert_not_awaited()
