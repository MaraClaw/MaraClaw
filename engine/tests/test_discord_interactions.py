import json
from unittest.mock import AsyncMock, Mock

import pytest
from nacl.signing import SigningKey
from tests.im_webhook_fixtures import AGENT_ID, channel_config, webhook_client

from app.api import background_tasks, discord_bot


@pytest.mark.parametrize("interaction_type", [1, 2])
@pytest.mark.parametrize(
    "failure",
    [
        None,
        "missing",
        "forged",
        "malformed",
        "missing_timestamp",
        "legacy",
        "invalid_key",
        "invalid_json",
        "modified_body",
    ],
)
async def test_discord_verifies_ping_and_commands_before_side_effects(monkeypatch, interaction_type, failure):
    signing_key = SigningKey.generate()
    config = channel_config("discord")
    config.encrypt_key = signing_key.verify_key.encode().hex()
    body = json.dumps(
        {
            "type": interaction_type,
            "data": {"name": "ask", "options": [{"name": "message", "value": "private-message"}]},
        }
    ).encode()
    if failure == "invalid_json":
        body = b"\xffinvalid-json"
    headers = {"x-signature-timestamp": "123", "x-signature-ed25519": signing_key.sign(b"123" + body).signature.hex()}
    if failure == "missing":
        headers = {}
    if failure == "forged":
        headers["x-signature-ed25519"] = SigningKey.generate().sign(b"123" + body).signature.hex()
    if failure == "malformed":
        headers["x-signature-ed25519"] = "zz" * 64
    if failure == "missing_timestamp":
        headers.pop("x-signature-timestamp")
    if failure == "legacy":
        config.encrypt_key = None
    if failure == "invalid_key":
        config.encrypt_key = "ff"
    if failure == "modified_body":
        body += b" "
    dispatched = []

    def schedule(coroutine, label):
        dispatched.append(label)
        coroutine.close()

    update, logs = AsyncMock(), Mock()
    monkeypatch.setattr(discord_bot.channel_config_dao, "get_for_agent", AsyncMock(return_value=config))
    monkeypatch.setattr(discord_bot.channel_config_dao, "update", update)
    monkeypatch.setattr(discord_bot, "logger", logs)
    monkeypatch.setattr(background_tasks, "schedule_background_task", schedule)
    async with webhook_client(discord_bot.router) as client:
        response = await client.post(f"/api/channel/discord/{AGENT_ID}/webhook", content=body, headers=headers)
    if failure is None:
        assert response.status_code == 200
        assert response.json()["type"] == (1 if interaction_type == 1 else 5)
        assert len(dispatched) == (0 if interaction_type == 1 else 1)
    else:
        assert response.status_code == 401
        assert not dispatched
        assert not update.called
        assert not logs.mock_calls


@pytest.mark.parametrize("configured", [False, True])
async def test_discord_interactions_are_absent_for_gateway_and_missing_rows(monkeypatch, configured):
    config = channel_config("discord") if configured else None
    if config:
        config.extra_config = {"connection_mode": "gateway"}
    monkeypatch.setattr(discord_bot.channel_config_dao, "get_for_agent", AsyncMock(return_value=config))
    async with webhook_client(discord_bot.router) as client:
        response = await client.post(f"/api/channel/discord/{AGENT_ID}/webhook", json={"type": 1})
    assert response.status_code == 404
