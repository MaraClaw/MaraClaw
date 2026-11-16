import json
from unittest.mock import AsyncMock

import anyio
import httpx
import pytest
from tests.telegram_config_fixtures import BOT_TOKEN, SECRET, configured_api

from app.core.domain.agents import PermissionAccessLevel
from app.core.domain.channels import ChannelType
from app.dao.channel_config_dao import channel_config_dao
from app.dao.channel_credentials import channel_credentials
from app.services.channels import config as config_service, telegram_setup
from app.services.platform_service import PublicBaseUrlNotConfiguredError, platform_service

__all__ = ["configured_api"]


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"ok": False, "description": BOT_TOKEN}),
        httpx.Response(400, json={"ok": False}),
        httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 1}}),
        httpx.Response(500, text=BOT_TOKEN),
        httpx.Response(302, headers={"Location": f"https://other.example/{BOT_TOKEN}"}),
        httpx.Response(200, json={"ok": False, "description": SECRET}),
        httpx.Response(200, json={"ok": True, "result": False}),
        httpx.Response(200, json={"ok": "true", "result": True}),
        httpx.Response(200, json={"ok": True, "result": "true"}),
        httpx.Response(200, json={"ok": True}),
        httpx.Response(200, json={"ok": True, "result": {"id": 1}}),
        httpx.Response(200, text=f"not JSON: {BOT_TOKEN} {SECRET}"),
    ],
)
async def test_registration_failure_does_not_persist_or_expose_credentials(
    configured_api, existing, response, caplog
) -> None:
    client, store, _, agent = configured_api
    if existing:
        await channel_config_dao.create(
            obj_in={
                "agent_id": agent.id,
                "channel_type": ChannelType.TELEGRAM,
                "app_id": "123456",
                "app_secret": BOT_TOKEN,
                "verification_token": SECRET,
                "is_configured": True,
            }
        )
    before = dict(store.row) if store.row is not None else None
    writes_before = len(store.writes)
    store.registration_response = response
    result = await client.post(f"/api/agents/{agent.id}/telegram-channel", json={"bot_token": "654321:replacement"})
    assert result.status_code == 502
    assert store.row == before
    assert len(store.writes) == writes_before
    store.invalidate.assert_not_awaited()
    assert len(store.registrations) == 1
    request = store.registrations[0]
    payload = json.loads(request.content)
    for sensitive in (BOT_TOKEN, SECRET, "654321:replacement", payload["secret_token"], payload["url"]):
        assert sensitive not in result.text + caplog.text


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ReadTimeout])
async def test_registration_transport_errors_are_secret_safe(configured_api, error_type, caplog) -> None:
    client, store, _, agent = configured_api
    store.registration_error = error_type
    result = await client.post(f"/api/agents/{agent.id}/telegram-channel", json={"bot_token": BOT_TOKEN})
    assert result.status_code == 502
    assert not store.writes
    payload = json.loads(store.registrations[0].content)
    for sensitive in (BOT_TOKEN, payload["secret_token"], payload["url"]):
        assert sensitive not in result.text + caplog.text


@pytest.mark.parametrize(
    "public_base",
    [
        "",
        "http://engine.example",
        "https:///missing-host",
        "https://engine.example:5173",
        "https://private:password@engine.example",
        "https://engine.example?private=query",
        "https://engine.example/#fragment",
        "https://localhost",
        "https://127.0.0.1",
        "https://[::1]",
    ],
)
async def test_unusable_public_url_rejects_before_registration(configured_api, monkeypatch, public_base) -> None:
    client, store, _, agent = configured_api
    monkeypatch.setattr(platform_service, "get_public_base_url", AsyncMock(return_value=public_base))
    result = await client.post(f"/api/agents/{agent.id}/telegram-channel", json={"bot_token": BOT_TOKEN})
    assert result.status_code == 503
    assert BOT_TOKEN not in result.text
    assert not store.registrations
    assert not store.writes


async def test_unconfigured_public_url_rejects_before_registration(configured_api, monkeypatch) -> None:
    client, store, _, agent = configured_api
    monkeypatch.setattr(
        platform_service, "get_public_base_url", AsyncMock(side_effect=PublicBaseUrlNotConfiguredError())
    )
    result = await client.post(f"/api/agents/{agent.id}/telegram-channel", json={"bot_token": BOT_TOKEN})
    assert result.status_code == 503
    assert not store.registrations
    assert not store.writes


@pytest.mark.parametrize("existing", [False, True])
async def test_overlapping_setup_preserves_one_registered_secret(configured_api, monkeypatch, existing) -> None:
    client, store, _, agent = configured_api
    if existing:
        await channel_config_dao.create(
            obj_in={
                "agent_id": agent.id,
                "channel_type": ChannelType.TELEGRAM,
                "app_id": "123456",
                "app_secret": BOT_TOKEN,
                "verification_token": SECRET,
                "is_configured": True,
            }
        )
        assert store.row is not None
        store.row["verification_token"] = "damaged-private-value"
    first_registered, second_authorized, release = anyio.Event(), anyio.Event(), anyio.Event()
    registrations: list[httpx.Request] = []
    responses: list[httpx.Response] = []

    async def authorize(_user, _agent_id):
        if first_registered.is_set():
            second_authorized.set()
        return agent, PermissionAccessLevel.MANAGE

    async def register(request: httpx.Request) -> httpx.Response:
        assert store.session_depth == 0
        registrations.append(request)
        first_registered.set()
        await release.wait()
        return httpx.Response(200, json={"ok": True, "result": True})

    async def save() -> None:
        responses.append(await client.post(f"/api/agents/{agent.id}/telegram-channel", json={"bot_token": BOT_TOKEN}))

    monkeypatch.setattr(config_service, "check_agent_access", authorize)
    async with httpx.AsyncClient(transport=httpx.MockTransport(register)) as provider:
        monkeypatch.setattr(telegram_setup, "outbound_client", lambda: provider)
        with anyio.fail_after(5):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(save)
                await first_registered.wait()
                tasks.start_soon(save)
                await second_authorized.wait()
                try:
                    assert len(registrations) == 1
                finally:
                    release.set()
    assert [response.status_code for response in responses] == [201, 201]
    assert responses[0].json()["id"] == responses[1].json()["id"]
    assert store.row is not None
    secret = channel_credentials(store.row, "open")["verification_token"]
    assert len(registrations) == 2
    assert all(json.loads(request.content)["secret_token"] == secret for request in registrations)
