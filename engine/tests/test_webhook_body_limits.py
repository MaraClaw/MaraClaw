import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from tests.im_webhook_fixtures import channel_config
from tests.webhook_intake_fixtures import SURFACES, ASGIInput, drive_asgi, webhook_path

from app.api import discord_bot, google_chat, slack, teams, webhooks, wecom, whatsapp
from app.services.channels import feishu_auth, submissions


def _known_generic_trigger(monkeypatch: pytest.MonkeyPatch, **config: str) -> tuple[AsyncMock, AsyncMock]:
    """Resolve every token to one trigger within its rate limit; return the lookup and the hit counter."""
    trigger = SimpleNamespace(id=uuid4(), agent_id=uuid4(), name="trigger", config=config)
    lookup = AsyncMock(return_value=trigger)
    counter = AsyncMock(return_value=1)
    monkeypatch.setattr(webhooks.agent_trigger_dao, "find_webhook_by_token", lookup)
    monkeypatch.setattr(webhooks, "_record_and_count_hits", counter)
    monkeypatch.setattr(webhooks.agent_dao, "get", AsyncMock(return_value=SimpleNamespace(webhook_rate_limit=5)))
    return lookup, counter


@pytest.mark.parametrize(("provider", "router", "cap", "auth_status"), SURFACES)
@pytest.mark.parametrize("framing", ["declared", "chunked", "understated"])
async def test_oversized_webhook_stops_before_auth_and_acceptance(
    monkeypatch, provider, router, cap, auth_status, framing
):
    # Given: each real route, with tripwires at every expensive boundary; the generic route resolves its trigger
    # and counts the hit first, so it is the one route whose lookup precedes the size rejection.
    app = FastAPI()
    app.include_router(router, prefix="" if provider == "generic" else "/api")
    blocked = AsyncMock(side_effect=AssertionError("Expensive work before size rejection"))
    signature = Mock(side_effect=AssertionError("Signature work before size rejection"))
    lookup, counter = _known_generic_trigger(monkeypatch)
    monkeypatch.setattr(webhooks, "get_redis", blocked)
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
    assert (lookup.await_count, counter.await_count) == ((1, 1) if provider == "generic" else (0, 0))
    blocked.assert_not_called()
    signature.assert_not_called()


@pytest.mark.parametrize(("provider", "router", "cap", "auth_status"), SURFACES)
@pytest.mark.parametrize("declared", [False, True])
async def test_exact_cap_reaches_normal_auth_rejection(monkeypatch, provider, router, cap, auth_status, declared):
    # Given: a configured route and exactly its cap of syntactically valid input.
    app = FastAPI()
    app.include_router(router, prefix="" if provider == "generic" else "/api")
    config = channel_config({"teams": "microsoft_teams", "generic": "slack"}.get(provider, provider))
    lookup = AsyncMock(return_value=config)
    monkeypatch.setattr(slack.channel_config_dao, "get_for_agent", lookup)
    monkeypatch.setattr(google_chat.channel_config, "get_channel_config", lookup)
    monkeypatch.setattr(google_chat.gchat, "audience_for_config", lambda _: "")
    # The generic route resolves a signed trigger before it reads the body, then rejects the unsigned request.
    known, _ = _known_generic_trigger(monkeypatch, secret="secret")
    monkeypatch.setattr(webhooks, "enqueue_webhook_execution", AsyncMock(side_effect=AssertionError("Unsigned run")))
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
    (known if provider == "generic" else lookup).assert_awaited_once()
    accepted.assert_not_called()
