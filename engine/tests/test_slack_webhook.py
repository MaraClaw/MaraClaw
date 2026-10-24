import hashlib
import hmac
from unittest.mock import AsyncMock

import pytest
from tests.im_webhook_fixtures import AGENT_ID, channel_config, webhook_client

from app.api import slack
from app.services.channels import slack_submissions

NOW = 1_700_000_000
SECRET = "app-secret"


@pytest.mark.parametrize(
    "failure",
    [None, "missing", "forged", "non_ascii", "timestamp", "huge_timestamp", "stale", "utf8", "json"],
)
async def test_slack_malformed_auth_rejects_before_acceptance(monkeypatch, failure):
    # Given: a real signed Slack challenge or a deliberately malformed auth input.
    body = b'{"type":"url_verification","challenge":"challenge"}'
    if failure == "utf8":
        body = b"\xff"
    if failure == "json":
        body = b"invalid json"
    timestamp = str(NOW)
    if failure == "timestamp":
        timestamp = "not-a-number"
    if failure == "huge_timestamp":
        timestamp = "9" * 400
    if failure == "stale":
        timestamp = str(NOW - 301)
    signature = (
        b"v0="
        + hmac.new(SECRET.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256).hexdigest().encode()
    )
    if failure == "forged":
        signature = b"v0=forged"
    if failure == "non_ascii":
        signature = b"\xff"
    headers = [(b"x-slack-request-timestamp", timestamp.encode()), (b"x-slack-signature", signature)]
    if failure == "missing":
        headers = []
    monkeypatch.setattr(slack.time, "time", lambda: NOW)
    monkeypatch.setattr(slack.channel_config_dao, "get_for_agent", AsyncMock(return_value=channel_config("slack")))
    accept = AsyncMock()
    monkeypatch.setattr(slack_submissions, "accept_slack_event", accept)

    # When: traversing the real webhook route with raw header bytes.
    async with webhook_client(slack.router) as client:
        response = await client.post(f"/api/channel/slack/{AGENT_ID}/webhook", content=body, headers=headers)

    # Then: malformed authentication never crashes or accepts a turn.
    assert response.status_code == (200 if failure is None else 401)
    if failure is None:
        assert response.json() == {"challenge": "challenge"}
    accept.assert_not_called()


async def test_slack_exact_cap_valid_signature_preserves_challenge(monkeypatch):
    # Given: a correctly signed challenge padded to the channel byte budget.
    body = b'{"type":"url_verification","challenge":"boundary"}'
    body += b" " * (1024 * 1024 - len(body))
    timestamp = str(NOW)
    digest = hmac.new(SECRET.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256).hexdigest()
    monkeypatch.setattr(slack.time, "time", lambda: NOW)
    monkeypatch.setattr(slack.channel_config_dao, "get_for_agent", AsyncMock(return_value=channel_config("slack")))

    # When: the actual webhook authenticates all bytes, including boundary padding.
    async with webhook_client(slack.router) as client:
        response = await client.post(
            f"/api/channel/slack/{AGENT_ID}/webhook",
            content=body,
            headers={"x-slack-request-timestamp": timestamp, "x-slack-signature": f"v0={digest}"},
        )

    # Then: an exact-cap authentic body is not mistaken for overflow.
    assert response.status_code == 200
    assert response.json() == {"challenge": "boundary"}
