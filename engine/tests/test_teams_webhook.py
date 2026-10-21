import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwk, jwt
from tests.im_webhook_fixtures import AGENT_ID, channel_config, webhook_client

from app.api import teams
from app.core.json_types import json_object_from, object_call
from app.services.channels import teams_auth

SERVICE_URL = "https://smba.trafficmanager.net/amer/"


@pytest.fixture(scope="module")
def signing_keys():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    public_jwk = json_object_from(object_call(jwk.construct(public_pem, algorithm="RS256").to_dict))
    return private_pem, {**public_jwk, "kid": "connector-key"}


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "missing",
        "malformed",
        "scheme",
        "forged",
        "issuer",
        "audience",
        "expired",
        "future",
        "missing_exp",
        "missing_nbf",
        "missing_service_claim",
        "service_mismatch",
        "service_host",
        "legacy",
        "unknown_kid",
        "algorithm",
        "invalid_json",
        "non_object",
        "missing_service",
        "keys_unavailable",
        "invalid_key",
        "malformed_jwt",
        "non_ascii_header",
    ],
)
async def test_teams_authenticates_before_persist_dedup_or_dispatch(monkeypatch, signing_keys, failure):
    private_key, public_key = signing_keys
    config = channel_config("microsoft_teams")
    activity = {
        "id": "teams-event",
        "type": "message",
        "serviceUrl": SERVICE_URL,
        "text": "private-message",
        "from": {"id": "customer", "name": "Customer"},
        "conversation": {"id": "conversation"},
    }
    now = int(time.time())
    claims = {
        "iss": teams_auth.BOT_FRAMEWORK_ISSUER,
        "aud": "bot-app",
        "nbf": now - 60,
        "exp": now + 600,
        "serviceUrl": SERVICE_URL,
    }
    if failure in {"issuer", "audience"}:
        claims["iss" if failure == "issuer" else "aud"] = "attacker"
    if failure in {"missing_exp", "missing_nbf", "missing_service_claim"}:
        claims.pop({"missing_exp": "exp", "missing_nbf": "nbf", "missing_service_claim": "serviceUrl"}[failure])
    if failure == "expired":
        claims["exp"] = now - 600
    if failure == "future":
        claims["nbf"] = now + 600
    if failure == "service_mismatch":
        activity["serviceUrl"] = "https://smba.trafficmanager.net/emea/"
    if failure == "service_host":
        activity["serviceUrl"] = claims["serviceUrl"] = "https://attacker.example/"
    if failure == "missing_service":
        activity.pop("serviceUrl")
    if failure == "legacy":
        config.app_id = None
    token = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "connector-key"})
    if failure == "forged":
        token = (
            jwt.encode(
                {**claims, "aud": "attacker"}, private_key, algorithm="RS256", headers={"kid": "connector-key"}
            ).rsplit(".", 1)[0]
            + "."
            + token.rsplit(".", 1)[1]
        )
    if failure == "unknown_kid":
        token = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "unknown"})
    if failure == "algorithm":
        token = jwt.encode(claims, "not-a-connector-key", algorithm="HS256", headers={"kid": "connector-key"})
    headers = {"authorization": "Bearer " + token}
    if failure == "missing":
        headers = {}
    if failure == "malformed":
        headers["authorization"] = "Bearer invalid.jwt.value extra"
    if failure == "scheme":
        headers["authorization"] = "Basic " + token
    if failure == "malformed_jwt":
        headers["authorization"] = "Bearer invalid.jwt.value"
    if failure == "non_ascii_header":
        headers = {b"authorization": b"Bearer \xff"}
    body = json.dumps(activity).encode()
    if failure == "invalid_json":
        body = b"\xffnot-json"
    if failure == "non_object":
        body = b"[]"
    fetch_keys = AsyncMock(return_value=[public_key])
    if failure == "keys_unavailable":
        fetch_keys.side_effect = httpx.ConnectError("Key endpoint unavailable")
    if failure == "invalid_key":
        fetch_keys.return_value = [{"kid": "connector-key", "kty": "RSA"}]
    seen, mark, update, dispatch, logs = (
        AsyncMock(return_value=False),
        AsyncMock(),
        AsyncMock(return_value=config),
        AsyncMock(),
        Mock(),
    )
    monkeypatch.setattr(teams_auth, "_fetch_signing_keys", fetch_keys)
    monkeypatch.setattr(teams.channel_config_dao, "get_for_agent", AsyncMock(return_value=config))
    monkeypatch.setattr(teams.channel_config_dao, "update", update)
    monkeypatch.setattr(teams.channel_dedup, "already_processed_shared", seen)
    monkeypatch.setattr(teams.channel_dedup, "mark_processed_shared", mark)
    monkeypatch.setattr(teams.channel_inbound, "load_agent", AsyncMock(return_value=SimpleNamespace(creator_id=None)))
    monkeypatch.setattr(
        teams.channel_inbound,
        "resolve_sender_user",
        AsyncMock(return_value=SimpleNamespace(id=AGENT_ID, display_name="Customer")),
    )
    monkeypatch.setattr(teams.channel_inbound, "open_channel_session", AsyncMock(return_value=object()))
    monkeypatch.setattr(
        teams.channel_inbound,
        "bind_channel_conversation",
        AsyncMock(
            return_value=SimpleNamespace(transcript_session=object(), reply_session=SimpleNamespace(id=AGENT_ID))
        ),
    )
    monkeypatch.setattr(teams.channel_inbound, "run_text_turn", dispatch)
    dispatch.return_value = "Answer"
    monkeypatch.setattr(teams, "_send_teams_message", AsyncMock())
    monkeypatch.setattr(teams, "logger", logs)
    async with webhook_client(teams.router) as client:
        response = await client.post(f"/api/channel/teams/{AGENT_ID}/webhook", content=body, headers=headers)
    if failure is None:
        assert response.status_code == 200
        assert response.json() == {"ok": True}
        assert update.call_args.kwargs["obj_in"]["extra_config"]["service_url"] == SERVICE_URL
        dispatch.assert_awaited_once()
        mark.assert_awaited_once_with("teams", "teams-event", cap=2000)
    else:
        assert response.status_code == 401
        assert not update.called
        assert not seen.called
        assert not mark.called
        assert not dispatch.called
        assert not logs.mock_calls


@pytest.mark.parametrize("skew", [-120, 120])
async def test_teams_allows_five_minutes_clock_skew(monkeypatch, signing_keys, skew):
    private_key, public_key = signing_keys
    now = int(time.time())
    claims = {
        "iss": teams_auth.BOT_FRAMEWORK_ISSUER,
        "aud": "bot-app",
        "nbf": now + skew,
        "exp": now + skew,
        "serviceUrl": SERVICE_URL,
    }
    token = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "connector-key"})
    monkeypatch.setattr(teams_auth, "_fetch_signing_keys", AsyncMock(return_value=[public_key]))
    monkeypatch.setattr(
        teams.channel_config_dao, "get_for_agent", AsyncMock(return_value=channel_config("microsoft_teams"))
    )
    monkeypatch.setattr(teams.channel_config_dao, "update", AsyncMock())
    async with webhook_client(teams.router) as client:
        response = await client.post(
            f"/api/channel/teams/{AGENT_ID}/webhook",
            json={"type": "conversationUpdate", "serviceUrl": SERVICE_URL},
            headers={"authorization": "Bearer " + token},
        )
    assert response.status_code == 200


async def test_teams_missing_channel_is_not_found(monkeypatch):
    monkeypatch.setattr(teams.channel_config_dao, "get_for_agent", AsyncMock(return_value=None))
    async with webhook_client(teams.router) as client:
        response = await client.post(f"/api/channel/teams/{AGENT_ID}/webhook", json={})
    assert response.status_code == 404
