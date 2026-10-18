from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI
from nacl.signing import SigningKey
from tests.im_webhook_fixtures import AGENT_ID, channel_config

from app.api import discord_bot, feishu, teams, whatsapp
from app.core.security import get_current_user

PROVIDERS = {
    "feishu": (feishu, "channel", {"app_id": "app", "app_secret": "secret"}, "verification_token"),
    "whatsapp": (
        whatsapp,
        "whatsapp-channel",
        {"access_token": "token", "phone_number_id": "phone", "verify_token": "verify"},
        "app_secret",
    ),
    "discord": (discord_bot, "discord-channel", {"application_id": "app", "bot_token": "token"}, "public_key"),
}


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("secret", [None, "", "  "])
async def test_webhook_config_refuses_missing_or_cleared_credentials(monkeypatch, provider, existing, secret):
    module, endpoint, required, field = PROVIDERS[provider]
    payload = dict(required)
    if secret is not None:
        payload[field] = secret
    monkeypatch.setattr(module, "check_agent_access", AsyncMock(return_value=(Mock(), None)))
    monkeypatch.setattr(module, "is_agent_creator", lambda *_: True)
    monkeypatch.setattr(
        module.channel_config_dao,
        "get_for_agent",
        AsyncMock(return_value=channel_config(provider) if existing else None),
    )
    create, update = AsyncMock(), AsyncMock()
    monkeypatch.setattr(module.channel_config_dao, "create", create)
    monkeypatch.setattr(module.channel_config_dao, "update", update)
    app = FastAPI()
    app.include_router(module.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: Mock()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/agents/{AGENT_ID}/{endpoint}", json=payload)
    app.dependency_overrides.clear()
    assert response.status_code == 422
    assert not create.called
    assert not update.called


@pytest.mark.parametrize("public_key", ["zz" * 32, "ff" * 31, "ff" * 33, "ff " * 32])
async def test_discord_config_requires_exact_hex_public_key(monkeypatch, public_key):
    monkeypatch.setattr(discord_bot, "check_agent_access", AsyncMock(return_value=(Mock(), None)))
    monkeypatch.setattr(discord_bot, "is_agent_creator", lambda *_: True)
    app = FastAPI()
    app.include_router(discord_bot.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: Mock()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/api/agents/{AGENT_ID}/discord-channel",
            json={"bot_token": "token", "application_id": "app", "public_key": public_key},
        )
    app.dependency_overrides.clear()
    assert response.status_code == 422


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("app_id", ["", "bot-app"])
async def test_teams_managed_identity_requires_and_persists_audience(monkeypatch, existing, app_id):
    config = channel_config("microsoft_teams")
    monkeypatch.setattr(teams, "check_agent_access", AsyncMock(return_value=(Mock(), None)))
    monkeypatch.setattr(teams, "is_agent_creator", lambda *_: True)
    monkeypatch.setattr(teams.channel_config_dao, "get_for_agent", AsyncMock(return_value=config if existing else None))
    create, update = AsyncMock(return_value=config), AsyncMock(return_value=config)
    monkeypatch.setattr(teams.channel_config_dao, "create", create)
    monkeypatch.setattr(teams.channel_config_dao, "update", update)
    app = FastAPI()
    app.include_router(teams.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: Mock()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/api/agents/{AGENT_ID}/teams-channel", json={"use_managed_identity": True, "app_id": app_id}
        )
    app.dependency_overrides.clear()
    assert response.status_code == (201 if app_id else 422)
    if app_id:
        assert (update if existing else create).call_args.kwargs["obj_in"]["app_id"] == app_id
    else:
        assert not create.called
        assert not update.called


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("existing", [False, True])
async def test_webhook_config_accepts_required_verification_credentials(monkeypatch, provider, existing):
    module, endpoint, required, field = PROVIDERS[provider]
    secret = SigningKey.generate().verify_key.encode().hex() if provider == "discord" else "provider-secret"
    config = channel_config(provider)
    monkeypatch.setattr(module, "check_agent_access", AsyncMock(return_value=(Mock(), None)))
    monkeypatch.setattr(module, "is_agent_creator", lambda *_: True)
    monkeypatch.setattr(
        module.channel_config_dao, "get_for_agent", AsyncMock(return_value=config if existing else None)
    )
    create, update = AsyncMock(return_value=config), AsyncMock(return_value=config)
    monkeypatch.setattr(module.channel_config_dao, "create", create)
    monkeypatch.setattr(module.channel_config_dao, "update", update)
    monkeypatch.setattr(discord_bot, "_register_slash_commands", AsyncMock(return_value={"status": 200, "body": ""}))
    monkeypatch.setattr(feishu, "schedule_background_task", lambda coroutine, _: coroutine.close())
    app = FastAPI()
    app.include_router(module.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: Mock()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/agents/{AGENT_ID}/{endpoint}", json={**required, field: secret})
    app.dependency_overrides.clear()
    assert response.status_code == 201
    stored_field = {"feishu": "verification_token", "whatsapp": "encrypt_key", "discord": "encrypt_key"}[provider]
    assert (update if existing else create).call_args.kwargs["obj_in"][stored_field] == secret


@pytest.mark.parametrize("provider", ["feishu", "discord"])
async def test_connection_modes_do_not_require_webhook_credentials(monkeypatch, provider):
    module, endpoint, required, _ = PROVIDERS[provider]
    config = channel_config(provider)
    mode = "websocket" if provider == "feishu" else "gateway"
    config.extra_config = {"connection_mode": mode}
    payload = (
        {**required, "extra_config": {"connection_mode": mode}}
        if provider == "feishu"
        else {**required, "connection_mode": mode}
    )
    monkeypatch.setattr(module, "check_agent_access", AsyncMock(return_value=(Mock(), None)))
    monkeypatch.setattr(module, "is_agent_creator", lambda *_: True)
    monkeypatch.setattr(module.channel_config_dao, "get_for_agent", AsyncMock(return_value=None))
    monkeypatch.setattr(module.channel_config_dao, "create", AsyncMock(return_value=config))
    monkeypatch.setattr(feishu, "schedule_background_task", lambda coroutine, _: coroutine.close())
    from app.services.discord_gateway import discord_gateway_manager

    monkeypatch.setattr(discord_gateway_manager, "start_client", AsyncMock())
    app = FastAPI()
    app.include_router(module.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: Mock()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/agents/{AGENT_ID}/{endpoint}", json=payload)
    app.dependency_overrides.clear()
    assert response.status_code == 201
