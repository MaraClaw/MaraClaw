import json
from unittest.mock import AsyncMock

import pytest
from tests.im_webhook_fixtures import AGENT_ID, channel_config, webhook_client

from app.api import google_chat
from app.services.channels import submissions


@pytest.mark.parametrize("token", ["verification-token", "", "forged", "é", "\ud800"])
async def test_google_chat_exact_cap_token_auth_rejects_non_ascii_without_crashing(monkeypatch, token):
    # Given: the legacy token flow and an exact-cap body with arbitrary token text.
    config = channel_config("google_chat")
    monkeypatch.setattr(google_chat.channel_config, "get_channel_config", AsyncMock(return_value=config))
    monkeypatch.setattr(google_chat.gchat, "audience_for_config", lambda _: "")
    accept = AsyncMock(side_effect=AssertionError("Unsupported or unauthenticated event must not enqueue"))
    monkeypatch.setattr(submissions, "accept_channel_event", accept)
    body = json.dumps({"token": token}).encode()
    body += b" " * (1024 * 1024 - len(body))

    # When: the real route parses and authenticates the boundary-size request.
    async with webhook_client(google_chat.router) as client:
        response = await client.post(f"/api/channel/google-chat/{AGENT_ID}/webhook", content=body)

    # Then: only the configured token is accepted; malformed text never produces a 500.
    assert response.status_code == (200 if token == "verification-token" else 401)
    accept.assert_not_called()
