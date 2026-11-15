from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api import telegram
from app.core.domain.agents import PermissionAccessLevel
from app.core.stored_credentials import StoredCredentialError
from app.dao.channel_config_dao import channel_config_dao
from app.dao.channel_credentials import channel_credentials
from app.records.agent import AgentRecord
from app.records.user import UserRecord
from app.services.channels import config as config_service
from app.services.platform_service import platform_service

BOT_TOKEN = "123456:private_bot-token"
SECRET = "private_webhook-secret"


@dataclass
class ConfigStore:
    row: dict[str, object] | None = None
    writes: list[dict[str, object]] = field(default_factory=list)

    async def fetchone(self, sql: str, params: Mapping[str, object]) -> dict[str, object] | None:
        if sql.startswith("INSERT"):
            self.row = {**params, "extra_config": {}}
            self.writes.append(dict(self.row))
        elif sql.startswith("UPDATE"):
            assert self.row is not None
            self.row = {**self.row, **params, "extra_config": {}}
            self.writes.append(dict(self.row))
        elif sql.startswith("DELETE"):
            row, self.row = self.row, None
            return row
        return dict(self.row) if self.row is not None else None


@pytest.fixture
async def configured_api(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[httpx.AsyncClient, ConfigStore, UserRecord, AgentRecord]]:
    store = ConfigStore()
    actor = UserRecord(id=uuid4())
    agent = AgentRecord(id=uuid4(), name="Telegram", creator_id=actor.id)

    @asynccontextmanager
    async def session() -> AsyncGenerator[ConfigStore]:
        yield store

    monkeypatch.setattr(channel_config_dao, "session", session)
    monkeypatch.setattr(telegram.channel_sender_link_dao, "invalidate_other_accounts", AsyncMock())
    monkeypatch.setattr(
        config_service, "check_agent_access", AsyncMock(return_value=(agent, PermissionAccessLevel.MANAGE))
    )
    monkeypatch.setattr(platform_service, "get_public_base_url", AsyncMock(return_value="https://engine.example"))
    app = FastAPI()
    app.include_router(telegram.router, prefix="/api")
    app.dependency_overrides[telegram.get_current_user] = lambda: actor
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client, store, actor, agent
    app.dependency_overrides.clear()


async def test_creator_crud_seals_preserves_rotates_and_redacts(configured_api) -> None:
    client, store, _, agent = configured_api
    path = f"/api/agents/{agent.id}/telegram-channel"
    created = await client.post(path, json={"bot_token": BOT_TOKEN, "secret_token": SECRET})
    assert created.status_code == 201
    assert created.json()["app_id"] == "123456"
    assert created.json()["app_secret"] == "***"
    assert created.json()["verification_token"] == "***"
    assert store.row is not None
    for field_name in ("app_secret", "verification_token"):
        assert str(store.row[field_name]).startswith("mcred:v1:")
    assert BOT_TOKEN not in repr(store.row)
    assert SECRET not in repr(store.row)
    opened = channel_credentials(store.row, "open")
    assert opened["app_secret"] == BOT_TOKEN
    assert opened["verification_token"] == SECRET
    config_id = created.json()["id"]
    partial = await client.post(path, json={"secret_token": "rotated_secret"})
    assert partial.status_code == 201
    assert partial.json()["id"] == config_id
    assert channel_credentials(store.row, "open")["app_secret"] == BOT_TOKEN
    assert channel_credentials(store.row, "open")["verification_token"] == "rotated_secret"
    assert (await client.post(path, json={})).status_code == 201
    read = await client.get(path)
    assert read.status_code == 200
    assert BOT_TOKEN not in read.text
    assert "rotated_secret" not in read.text
    url = await client.get(path + "/webhook-url")
    assert url.json() == {"webhook_url": f"https://engine.example/api/channel/telegram/{config_id}/webhook"}
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


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"bot_token": BOT_TOKEN},
        {"secret_token": SECRET},
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


@pytest.mark.parametrize("secret", ["A", "a" * 256, "Az09_-"])
async def test_secret_token_ascii_boundaries_are_accepted(configured_api, secret: str) -> None:
    client, _, _, agent = configured_api
    response = await client.post(
        f"/api/agents/{agent.id}/telegram-channel", json={"bot_token": BOT_TOKEN, "secret_token": secret}
    )
    assert response.status_code == 201


async def test_damaged_credentials_can_be_read_deleted_or_fully_replaced(configured_api) -> None:
    client, store, _, agent = configured_api
    path = f"/api/agents/{agent.id}/telegram-channel"
    assert (await client.post(path, json={"bot_token": BOT_TOKEN, "secret_token": SECRET})).status_code == 201
    assert store.row is not None
    store.row["app_secret"] = "damaged-private-value"
    assert (await client.get(path)).status_code == 200
    partial = await client.post(path, json={"secret_token": "new_secret"})
    assert partial.status_code == 422
    assert "damaged-private-value" not in partial.text
    assert (await client.post(path, json={"bot_token": BOT_TOKEN, "secret_token": SECRET})).status_code == 201
    assert channel_credentials(store.row, "open")["app_secret"] == BOT_TOKEN
    store.row["app_secret"] = "damaged-private-value"
    with pytest.raises(StoredCredentialError):
        channel_credentials(store.row, "open")
    assert (await client.delete(path)).status_code == 204


async def test_configuration_requires_authenticated_user() -> None:
    app = FastAPI()
    app.include_router(telegram.router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/agents/{uuid4()}/telegram-channel")
    assert response.status_code == 401
