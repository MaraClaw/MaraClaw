import json
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from tests.im_webhook_fixtures import channel_config
from tests.webhook_intake_fixtures import SURFACES, ASGIInput, drive_asgi, webhook_path

from app.api import discord_bot, google_chat, slack, teams, webhooks, wecom, whatsapp
from app.services.channels import feishu_auth, submissions


@pytest.mark.parametrize(("provider", "router", "cap", "auth_status"), SURFACES)
@pytest.mark.parametrize("framing", ["declared", "chunked", "understated"])
async def test_oversized_webhook_stops_before_lookup_auth_and_acceptance(
    monkeypatch, provider, router, cap, auth_status, framing
):
    # Given: each real route, with tripwires at every expensive boundary.
    app = FastAPI()
    app.include_router(router, prefix="" if provider == "generic" else "/api")
    blocked = AsyncMock(side_effect=AssertionError("Expensive work before size rejection"))
    signature = Mock(side_effect=AssertionError("Signature work before size rejection"))
    monkeypatch.setattr(webhooks, "get_redis", blocked)
    monkeypatch.setattr(webhooks.agent_trigger_dao, "find_webhook_by_token", blocked)
    monkeypatch.setattr(slack.channel_config_dao, "get_for_agent", blocked)
    monkeypatch.setattr(google_chat.channel_config, "get_channel_config", blocked)
    monkeypatch.setattr(webhooks, "enqueue_webhook_execution", blocked)
    monkeypatch.setattr(submissions, "accept_channel_event", blocked)
    monkeypatch.setattr(slack, "_verify_slack_signature", signature)
    monkeypatch.setattr(whatsapp, "_verify_signature", signature)
    monkeypatch.setattr(google_chat.gchat, "verify_google_chat_bearer", blocked)
    monkeypatch.setattr(feishu_auth, "authenticate_feishu_callback", signature)
    monkeypatch.setattr(teams, "authenticate_teams_activity", blocked)
    monkeypatch.setattr(discord_bot, "_verify_discord_signature", signature)
    monkeypatch.setattr(wecom, "_parse_wecom_xml", signature)
    headers = ((b"content-length", str(cap + 1).encode()),) if framing == "declared" else ()
    if framing == "understated":
        headers = ((b"content-length", b"1"),)
    chunks = (b"x" * cap, b"x", b"MUST REMAIN UNREAD")

    # When: overflow is declared or arrives without a trustworthy length.
    response = await drive_asgi(app, ASGIInput(webhook_path(provider), chunks, headers))

    # Then: stop before reading declared overflow, or on the first exceeding chunk.
    assert response.status == 413
    assert response.reads == (0 if framing == "declared" else 2)
    if provider == "generic":
        assert json.loads(response.body) == {"ok": True}
    blocked.assert_not_called()
    signature.assert_not_called()


@pytest.mark.parametrize(("provider", "router", "cap", "auth_status"), SURFACES)
@pytest.mark.parametrize("declared", [False, True])
async def test_exact_cap_reaches_normal_auth_rejection(monkeypatch, provider, router, cap, auth_status, declared):
    # Given: a configured route and exactly its cap of syntactically valid input.
    app = FastAPI()
    app.include_router(router, prefix="" if provider == "generic" else "/api")
    config = channel_config("microsoft_teams" if provider == "teams" else provider)
    lookup = AsyncMock(return_value=config)
    monkeypatch.setattr(slack.channel_config_dao, "get_for_agent", lookup)
    monkeypatch.setattr(google_chat.channel_config, "get_channel_config", lookup)
    monkeypatch.setattr(google_chat.gchat, "audience_for_config", lambda _: "")
    unknown = AsyncMock(return_value=None)
    monkeypatch.setattr(webhooks.agent_trigger_dao, "find_webhook_by_token", unknown)
    monkeypatch.setattr(webhooks, "_record_and_count_hits", AsyncMock(return_value=1))
    accepted = AsyncMock(side_effect=AssertionError("Unauthenticated acceptance"))
    monkeypatch.setattr(submissions, "accept_channel_event", accepted)
    payload = b"<xml><Encrypt>ciphertext</Encrypt></xml>" if provider == "wecom" else b"{}"
    payload += b" " * (cap - len(payload))
    headers = ((b"content-length", str(cap).encode()),) if declared else ()

    # When: the full exact-cap request reaches the actual route.
    response = await drive_asgi(app, ASGIInput(webhook_path(provider), (payload[:17], payload[17:]), headers))

    # Then: no size rejection replaces the provider's normal auth behavior.
    assert response.status == auth_status
    assert response.reads == 2
    (unknown if provider == "generic" else lookup).assert_awaited_once()
    accepted.assert_not_called()
