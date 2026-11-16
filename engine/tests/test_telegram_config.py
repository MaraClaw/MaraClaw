import json
import re
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from tests.telegram_config_fixtures import BOT_TOKEN, SECRET, configured_api

from app.api import telegram
from app.core.domain.channels import ChannelType
from app.core.stored_credentials import StoredCredentialError
from app.dao.channel_config_dao import channel_config_dao
from app.dao.channel_credentials import channel_credentials

__all__ = ["configured_api"]


async def test_bot_token_only_registers_seals_preserves_and_redacts(configured_api, caplog) -> None:
    client, store, _, agent = configured_api
    path = f"/api/agents/{agent.id}/telegram-channel"
    created = await client.post(path, json={"bot_token": BOT_TOKEN})
    assert created.status_code == 201
    assert created.json()["app_id"] == "123456"
    assert created.json()["app_secret"] == "***"
    assert created.json()["verification_token"] == "***"
    assert store.row is not None
    for field_name in ("app_secret", "verification_token"):
        assert str(store.row[field_name]).startswith("mcred:v1:")
    assert BOT_TOKEN not in repr(store.row)
    opened = channel_credentials(store.row, "open")
    assert opened["app_secret"] == BOT_TOKEN
    secret = opened["verification_token"]
    assert isinstance(secret, str)
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", secret)
    assert secret not in repr(store.row)
    config_id = created.json()["id"]
    request = store.registrations[0]
    assert request.method == "POST"
    assert request.url.host == "api.telegram.org"
    assert request.url.path == f"/bot{BOT_TOKEN}/setWebhook"
    assert BOT_TOKEN not in str(request.url)
    assert BOT_TOKEN not in repr(request.url)
    assert json.loads(request.content) == {
        "url": f"https://engine.example/api/channel/telegram/{config_id}/webhook",
        "secret_token": secret,
        "allowed_updates": ["message"],
    }
    replacement = "654321:new_bot-token"
    partial = await client.post(path, json={"bot_token": replacement})
    assert partial.status_code == 201
    assert partial.json()["id"] == config_id
    assert channel_credentials(store.row, "open")["app_secret"] == replacement
    assert channel_credentials(store.row, "open")["verification_token"] == secret
    store.invalidate.assert_awaited_with(store.row["id"], "654321")
    assert (await client.post(path, json={})).status_code == 201
    read = await client.get(path)
    assert read.status_code == 200
    assert BOT_TOKEN not in read.text
    assert replacement not in read.text
    assert secret not in read.text + created.text + partial.text + caplog.text
    assert all(json.loads(request.content)["secret_token"] == secret for request in store.registrations)
    url = await client.get(path + "/webhook-url")
    assert url.json() == {"webhook_url": f"https://engine.example/api/channel/telegram/{config_id}/webhook"}
    webhook = f"/api/channel/telegram/{config_id}/webhook"
    assert (await client.post(webhook, json={"update_id": 1})).status_code == 401
    accepted = await client.post(webhook, json={"update_id": 1}, headers={"X-Telegram-Bot-Api-Secret-Token": secret})
    assert accepted.status_code == 200
    assert accepted.json() == {"ok": True}
    assert (await client.delete(path)).status_code == 204
    assert store.row is None


@pytest.mark.parametrize(("method", "suffix"), [("GET", ""), ("POST", ""), ("DELETE", ""), ("GET", "/webhook-url")])
async def test_every_configuration_operation_requires_creator(configured_api, method: str, suffix: str) -> None:
    client, store, actor, agent = configured_api
    actor.id = uuid4()
    response = await client.request(method, f"/api/agents/{agent.id}/telegram-channel{suffix}", json={})
    assert response.status_code == 403
    assert store.row is None
    assert not store.writes
    assert not store.registrations


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"bot_token": None},
        {"bot_token": ""},
        {"bot_token": 123456},
        {"bot_token": "0:invalid"},
        {"bot_token": "123:secret/path"},
        {"bot_token": "123:secret\n"},
        {"bot_token": "123:" + "a" * 509},
        {"secret_token": SECRET},
        {"bot_token": BOT_TOKEN, "secret_token": SECRET},
        {"bot_token": BOT_TOKEN, "secret_token": None},
        {"bot_token": BOT_TOKEN, "secret_token": "bad\nsecret"},
        {"bot_token": BOT_TOKEN, "secret_token": "non-ascii-\u00e9"},
        {"bot_token": BOT_TOKEN, "secret_token": "s" * 257},
        {"bot_token": "123:secret/path", "secret_token": SECRET},
        {"bot_token": BOT_TOKEN, "secret_token": 42},
        {"bot_token": BOT_TOKEN, "secret_token": SECRET, "endpoint": "https://other.example"},
    ],
)
async def test_invalid_or_incomplete_credentials_never_write_or_echo(
    configured_api, payload: dict[str, object]
) -> None:
    client, store, _, agent = configured_api
    response = await client.post(f"/api/agents/{agent.id}/telegram-channel", json=payload)
    assert response.status_code == 422
    assert BOT_TOKEN not in response.text
    assert SECRET not in response.text
    assert not store.writes
    assert not store.registrations


@pytest.mark.parametrize("secret", ["A", "a" * 256, "Az09_-"])
async def test_existing_manual_secret_is_preserved(configured_api, secret: str) -> None:
    client, store, _, agent = configured_api
    legacy = await channel_config_dao.create(
        obj_in={
            "agent_id": agent.id,
            "channel_type": ChannelType.TELEGRAM,
            "app_id": "123456",
            "app_secret": BOT_TOKEN,
            "verification_token": secret,
            "is_configured": True,
        }
    )
    response = await client.post(f"/api/agents/{agent.id}/telegram-channel", json={})
    assert response.status_code == 201
    assert response.json()["id"] == str(legacy.id)
    assert store.row is not None
    assert channel_credentials(store.row, "open")["verification_token"] == secret
    assert json.loads(store.registrations[0].content)["secret_token"] == secret


@pytest.mark.parametrize(
    "damaged_fields", [("app_secret",), ("verification_token",), ("app_secret", "verification_token")]
)
async def test_damaged_credentials_can_be_read_deleted_or_replaced(configured_api, damaged_fields) -> None:
    client, store, _, agent = configured_api
    path = f"/api/agents/{agent.id}/telegram-channel"
    assert (await client.post(path, json={"bot_token": BOT_TOKEN})).status_code == 201
    assert store.row is not None
    secret = channel_credentials(store.row, "open")["verification_token"]
    for field_name in damaged_fields:
        store.row[field_name] = "damaged-private-value"
    assert (await client.get(path)).status_code == 200
    partial = await client.post(path, json={})
    assert partial.status_code == 422
    assert "damaged-private-value" not in partial.text
    assert len(store.registrations) == len(store.writes) == 1
    assert (await client.post(path, json={"bot_token": BOT_TOKEN})).status_code == 201
    assert channel_credentials(store.row, "open")["app_secret"] == BOT_TOKEN
    repaired_secret = channel_credentials(store.row, "open")["verification_token"]
    assert (repaired_secret == secret) == ("verification_token" not in damaged_fields)
    store.row["app_secret"] = "damaged-private-value"
    with pytest.raises(StoredCredentialError):
        channel_credentials(store.row, "open")
    assert (await client.delete(path)).status_code == 204


@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_configuration_requires_authenticated_user(method: str) -> None:
    app = FastAPI()
    app.include_router(telegram.router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request(
            method, f"/api/agents/{uuid4()}/telegram-channel", json={"bot_token": BOT_TOKEN}
        )
    assert response.status_code == 401
