from unittest.mock import AsyncMock

import httpx
import pytest
from tests.im_webhook_fixtures import channel_config

from app.api import teams
from app.services.channels import teams_auth


@pytest.mark.parametrize(
    "service_url",
    [
        "https://attacker.example/",
        "http://smba.trafficmanager.net/",
        "https://smba.trafficmanager.net.attacker.example/",
        "https://smba.trafficmanager.net@attacker.example/",
        "https://user@smba.trafficmanager.net/",
        "https://smba.trafficmanager.net:444/",
        "https://127.0.0.1/",
        "https://smba.trafficmanager.net/?redirect=x",
        "https://smba.trafficmanager.net/#fragment",
        "https://smba.trafficmanager.net\\@attacker.example/",
        "",
    ],
)
async def test_teams_refuses_poisoned_stored_service_url_before_getting_token(monkeypatch, service_url):
    config = channel_config("microsoft_teams")
    config.extra_config = {"service_url": service_url}
    token = AsyncMock()
    send = AsyncMock()
    monkeypatch.setattr(teams, "_get_teams_access_token", token)
    monkeypatch.setattr(teams, "_send_teams_message_single_chunk", send)
    with pytest.raises(ValueError, match="Invalid Bot Framework service URL"):
        await teams._send_teams_message(config, "conversation", {"text": "Reply"})
    assert not token.called
    assert not send.called


@pytest.mark.parametrize("host", sorted(teams_auth._SERVICE_HOSTS))
async def test_teams_sends_to_official_service_hosts(monkeypatch, host):
    config = channel_config("microsoft_teams")
    service_url = f"https://{host}/amer/"
    config.extra_config = {"service_url": service_url}
    send = AsyncMock()
    monkeypatch.setattr(teams, "_get_teams_access_token", AsyncMock(return_value="bot-bearer"))
    monkeypatch.setattr(teams, "_send_teams_message_single_chunk", send)
    await teams._send_teams_message(config, "conversation", {"text": "Reply"})
    assert send.call_args.args[:3] == ("bot-bearer", service_url, "conversation")


async def test_teams_caches_keys_from_fixed_bot_framework_endpoint(monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"keys": [{"kid": "key", "kty": "RSA"}]})

    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(teams_auth, "_keys", [])
    monkeypatch.setattr(teams_auth, "_keys_expires_at", 0)
    monkeypatch.setattr(teams_auth.httpx, "AsyncClient", lambda **kwargs: client_type(transport=transport, **kwargs))
    first = await teams_auth._fetch_signing_keys()
    second = await teams_auth._fetch_signing_keys()
    assert first == second == [{"kid": "key", "kty": "RSA"}]
    assert [str(request.url) for request in requests] == ["https://login.botframework.com/v1/.well-known/keys"]
