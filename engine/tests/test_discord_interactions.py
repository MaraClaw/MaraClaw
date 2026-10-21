import json
from unittest.mock import AsyncMock, Mock

import pytest
from httpx import Response
from nacl.signing import SigningKey
from tests.im_webhook_fixtures import AGENT_ID, channel_config, webhook_client

from app.api import discord_bot
from app.services.channels import submissions


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
            "id": "interaction-1",
            "channel_id": "channel-1",
            "guild_id": "guild-1",
            "member": {"user": {"id": "sender-1"}},
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
    accept = AsyncMock(return_value=submissions.AcceptedChannelEvent(replayed=False))
    acceptance_at_response: list[int] = []

    async def observe_response(_response: Response) -> None:
        acceptance_at_response.append(accept.await_count)

    update, logs = AsyncMock(), Mock()
    monkeypatch.setattr(discord_bot.channel_config_dao, "get_for_agent", AsyncMock(return_value=config))
    monkeypatch.setattr(discord_bot.channel_config_dao, "update", update)
    monkeypatch.setattr(discord_bot, "logger", logs)
    monkeypatch.setattr(submissions, "accept_channel_event", accept)
    async with webhook_client(discord_bot.router) as client:
        client.event_hooks["response"].append(observe_response)
        response = await client.post(f"/api/channel/discord/{AGENT_ID}/webhook", content=body, headers=headers)
    if failure is None:
        assert response.status_code == 200
        assert response.json()["type"] == (1 if interaction_type == 1 else 5)
    else:
        assert response.status_code == 401
    if failure is None and interaction_type == 2:
        accept.assert_awaited_once_with(
            submissions.ChannelEvent(
                config_id=config.id,
                agent_id=AGENT_ID,
                provider="discord",
                event_key=json.dumps([config.app_id, "channel-1", "interaction-1"], separators=(",", ":")),
                sender_id="sender-1",
                conversation_id="discord_channel-1",
                text="private-message",
                kind="application_command",
                is_group=True,
            )
        )
        assert acceptance_at_response == [1]
    else:
        accept.assert_not_called()
        assert acceptance_at_response == [0]
    update.assert_not_called()
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
