from contextlib import suppress
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.services.channel_user_service import ChannelUserResolutionError
from app.services.channels import binding
from channel_shared_webhook_fixtures import (
    ACK_ONLY,
    WEBHOOKS,
    context as context,
    invoke_webhook,
    pipeline as pipeline,
    webhook_pipeline as webhook_pipeline,
)


@pytest.mark.parametrize("webhook", WEBHOOKS)
async def test_direct_webhook_binds_before_history_and_attachment_persistence(webhook_pipeline, monkeypatch, webhook):
    # Given
    pipeline, _ = webhook_pipeline
    _, _, shared, route, _, history, persist, enqueue = pipeline
    # When
    await invoke_webhook(webhook, webhook_pipeline, monkeypatch)
    # Then
    assert history.await_args.kwargs["conversation_id"] == str(shared.id)
    assert persist.await_args.kwargs["conversation_id"] == str(shared.id)
    if webhook in ACK_ONLY:
        enqueue.assert_not_awaited()
    else:
        assert enqueue.await_args.kwargs["conversation_id"] == str(shared.id)
        assert enqueue.await_args.kwargs["reply_session_id"] == route.id


@pytest.mark.parametrize("webhook", [name for name in WEBHOOKS if name != "whatsapp"])
async def test_group_webhook_keeps_provider_conversation(webhook_pipeline, monkeypatch, webhook):
    # Given
    pipeline, _ = webhook_pipeline
    _, _, _, route, _, history, persist, _ = pipeline
    # When
    await invoke_webhook(webhook, webhook_pipeline, monkeypatch, group=True)
    # Then
    assert history.await_args.kwargs["conversation_id"] == str(route.id)
    assert persist.await_args.kwargs["conversation_id"] == str(route.id)
    assert isinstance(binding.ensure_shared_session, AsyncMock)
    binding.ensure_shared_session.assert_not_awaited()


@pytest.mark.parametrize("webhook", WEBHOOKS)
@pytest.mark.parametrize("rejection", ["unbound", "unauthorized"])
async def test_rejected_webhook_cannot_access_shared_transcript(webhook_pipeline, monkeypatch, webhook, rejection):
    # Given
    pipeline, _ = webhook_pipeline
    *_, history, persist, enqueue = pipeline
    if rejection == "unbound":
        monkeypatch.setattr(binding.channel_user_service, "_find_org_member", AsyncMock(return_value=None))
    else:
        monkeypatch.setattr(binding, "check_agent_access", AsyncMock(side_effect=HTTPException(403)))
    # When
    with suppress(ChannelUserResolutionError, HTTPException):
        await invoke_webhook(webhook, webhook_pipeline, monkeypatch)
    # Then
    history.assert_not_awaited()
    persist.assert_not_awaited()
    enqueue.assert_not_awaited()
