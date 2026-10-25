from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from tests.channel_shared_webhook_fixtures import (
    WEBHOOKS,
    context,
    invoke_webhook,
    pipeline,
    webhook_pipeline,
)
from tests.test_channel_shared_processors import PROCESSORS, invoke_processor

from app.api import dingtalk
from app.services.channels import feishu_submissions, slack_submissions, submissions
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
