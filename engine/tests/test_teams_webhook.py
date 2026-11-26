import asyncio
import hashlib
import json
import logging
import sys
import time
from collections.abc import Callable, Coroutine, Iterator
from types import SimpleNamespace
from typing import final
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwk, jwt
from tests.im_webhook_fixtures import AGENT_ID, channel_config, webhook_client

from app.api import teams
from app.core.json_types import JsonObject, json_object_from, object_call
from app.records.channel_config import ChannelConfigRecord
from app.services.channels import dedup, submissions, teams_auth
from im_token_cache_fakes import CacheKey, TeamsWire, TokenCache, captured_app_log, install_token_cache

SERVICE_URL = "https://smba.trafficmanager.net/amer/"
CANARY = "canary-provider-secret-5b1c"
CONVERSATION = "19:conversation"


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
    monkeypatch.setattr(dedup, "already_processed_shared", seen)
    monkeypatch.setattr(dedup, "mark_processed_shared", mark)
    monkeypatch.setattr(submissions, "accept_channel_event", dispatch)
    monkeypatch.setattr(teams, "_send_teams_message", AsyncMock())
    monkeypatch.setattr(teams, "logger", logs)
    async with webhook_client(teams.router) as client:
        response = await client.post(f"/api/channel/teams/{AGENT_ID}/webhook", content=body, headers=headers)
    if failure is None:
        assert response.status_code == 200
        assert response.json() == {"ok": True}
        assert update.call_args.kwargs["obj_in"]["extra_config"]["service_url"] == SERVICE_URL
        dispatch.assert_awaited_once()
        mark.assert_not_awaited()
        assert "private-message" not in repr(logs.mock_calls)
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


@pytest.fixture
def app_log() -> Iterator[list[str]]:
    with captured_app_log() as chunks:
        yield chunks


def _app_output(chunks: list[str]) -> str:
    """Records from app modules only; the HTTP clients' own request logs are not under test."""
    return "\n".join(line for line in "".join(chunks).splitlines() if " | app." in line)


def _provider(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response]
    | Callable[[httpx.Request], Coroutine[None, None, httpx.Response]],
) -> TokenCache:
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        teams.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs)
    )
    return install_token_cache(monkeypatch)


def _reply(status: int, *, body: object = None, text: str = "") -> Callable[[httpx.Request], httpx.Response]:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body) if body is not None else httpx.Response(status, text=text)

    return handler


def _unreachable(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError(f"connect failed client_secret={CANARY}", request=request)


@pytest.mark.parametrize(
    ("handler", "expected"),
    [
        pytest.param(
            _reply(401, body={"error": "invalid_client", "error_description": f"AADSTS7000215 {CANARY}"}),
            "kind=http_status status=401 error_code=invalid_client",
            id="http-status-known-code",
        ),
        pytest.param(
            _reply(400, body={"error": CANARY, "error_description": CANARY}),
            "kind=http_status status=400 error_code=other",
            id="http-status-unlisted-code",
        ),
        pytest.param(
            _reply(502, text=f"<html>{CANARY}</html>"),
            "kind=http_status status=502 error_code=unparsed",
            id="http-status-unparsed-body",
        ),
        pytest.param(
            _reply(200, body={"access_token": 1234, "expires_in": CANARY}),
            "kind=malformed_response",
            id="malformed-fields",
        ),
        pytest.param(_reply(200, text=f"not json {CANARY}"), "kind=malformed_response", id="malformed-json"),
        pytest.param(_unreachable, "kind=transport error_type=ConnectError", id="transport"),
    ],
)
async def test_oauth_failure_logs_bounded_classification_never_provider_text(monkeypatch, app_log, handler, expected):
    config = channel_config("microsoft_teams")
    config.app_id, config.app_secret = f"{CANARY}-client-id", f"{CANARY}-client-secret"
    _provider(monkeypatch, handler)

    token = await teams._get_teams_access_token(config)

    output = _app_output(app_log)
    assert token is None
    assert f"Teams: OAuth token request failed agent={config.agent_id} {expected}" in output
    assert CANARY not in output


async def test_oauth_success_returns_the_token_without_logging_it(monkeypatch, app_log):
    _provider(monkeypatch, _reply(200, body={"access_token": f"{CANARY}-token", "expires_in": 3600}))

    token = await teams._get_teams_access_token(channel_config("microsoft_teams"))

    assert token == f"{CANARY}-token"
    assert "Successfully obtained access token" in _app_output(app_log)
    assert CANARY not in _app_output(app_log)


async def _send(*, strict: bool) -> None:
    activity: JsonObject = {"type": "message", "text": "hello"}
    await teams._send_teams_message_single_chunk(f"{CANARY}-bearer", SERVICE_URL, CONVERSATION, activity, strict=strict)


@pytest.mark.parametrize(
    ("handler", "strict", "error", "expected"),
    [
        pytest.param(
            _reply(403, body={"error": {"code": "BotNotInConversationRoster", "message": CANARY}}),
            False,
            httpx.HTTPStatusError,
            "kind=http_status status=403 error_code=BotNotInConversationRoster",
            id="http-status-known-code",
        ),
        pytest.param(
            _reply(500, text=CANARY),
            False,
            httpx.HTTPStatusError,
            "kind=http_status status=500 error_code=unparsed",
            id="http-status-unparsed-body",
        ),
        pytest.param(
            _reply(200, text=f"not json {CANARY}"), True, ValueError, "kind=malformed_response", id="malformed-json"
        ),
        pytest.param(
            _reply(200, body={"note": CANARY}), True, RuntimeError, "kind=malformed_response", id="malformed-missing-id"
        ),
        pytest.param(_unreachable, False, httpx.ConnectError, "kind=transport error_type=ConnectError", id="transport"),
    ],
)
async def test_send_failure_keeps_its_exception_and_logs_bounded_classification(
    monkeypatch, app_log, handler, strict, error, expected
):
    _provider(monkeypatch, handler)

    with pytest.raises(error):
        await _send(strict=strict)

    output = _app_output(app_log)
    assert f"Teams: send failed {expected} conversation={CONVERSATION}" in output
    assert CANARY not in output


async def test_strict_send_with_an_activity_id_succeeds_quietly(monkeypatch, app_log):
    _provider(monkeypatch, _reply(200, body={"id": "activity-1"}))

    await _send(strict=True)

    assert f"Teams: Sent message to conversation {CONVERSATION}" in _app_output(app_log)
    assert "failed" not in _app_output(app_log)


async def test_handler_failure_logs_the_exception_class_and_never_the_inbound_text(monkeypatch, app_log):
    config = channel_config("microsoft_teams")
    activity = {
        "id": "teams-event",
        "type": "message",
        "text": f"{CANARY} private message",
        "from": {"id": "customer"},
        "conversation": {"id": "conversation"},
    }
    monkeypatch.setattr(teams.channel_config_dao, "get_for_agent", AsyncMock(return_value=config))
    monkeypatch.setattr(teams, "authenticate_teams_activity", AsyncMock(return_value=activity))
    failing = AsyncMock(side_effect=RuntimeError(f"insert failed for {CANARY}"))
    monkeypatch.setattr(submissions, "accept_channel_event", failing)

    async with webhook_client(teams.router) as client:
        response = await client.post(f"/api/channel/teams/{AGENT_ID}/webhook", json=activity)

    output = _app_output(app_log)
    assert response.status_code == 500
    assert f"Teams: webhook handler failed agent={AGENT_ID} error_type=RuntimeError" in output
    assert f"[Teams] Message from=customer, conversation=conversation, chars={len(activity['text'])}" in output
    assert CANARY not in output


@pytest.fixture
def no_default_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEAMS_TENANT_ID", raising=False)


def _sole_entry(cache: TokenCache) -> tuple[CacheKey, str]:
    ((key, token),) = cache.items()
    return key, token


@pytest.mark.usefixtures("no_default_tenant")
async def test_concurrent_callers_share_one_client_credentials_mint_cached_until_expiry(monkeypatch):
    wire = TeamsWire()
    wire.gate.clear()
    cache = _provider(monkeypatch, wire)
    config = channel_config("microsoft_teams")
    callers = [asyncio.create_task(teams._get_teams_access_token(config)) for _ in range(5)]
    async with asyncio.timeout(5):
        await wire.started.wait()
    wire.gate.set()

    assert await asyncio.gather(*callers) == ["token-1"] * 5
    assert await teams._get_teams_access_token(config) == "token-1"
    assert len(wire.logins) == 1
    assert wire.logins[0].url.path == "/common/oauth2/v2.0/token"
    key, token = _sole_entry(cache)
    provider, app_id, secret = key
    assert (provider, app_id, token) == ("teams", "bot-app", "token-1")
    # The cache sees the tenant and a digest of the client secret, never the secret itself.
    assert "common" in secret
    assert hashlib.sha256(b"access-token").hexdigest() in secret
    assert "access-token" not in secret
    # The reported lifetime less the shared refresh skew.
    assert cache.ttls[key] == 3300


@pytest.mark.usefixtures("no_default_tenant")
async def test_a_rotated_secret_or_another_tenant_never_reuses_a_token(monkeypatch):
    wire = TeamsWire()
    cache = _provider(monkeypatch, wire)
    config = channel_config("microsoft_teams")

    assert await teams._get_teams_access_token(config) == "token-1"
    config.app_secret = "rotated-secret"
    assert await teams._get_teams_access_token(config) == "token-2"
    config.extra_config = {"tenant_id": "contoso"}
    assert await teams._get_teams_access_token(config) == "token-3"
    assert await teams._get_teams_access_token(config) == "token-3"

    assert [request.url.path for request in wire.logins] == [
        "/common/oauth2/v2.0/token",
        "/common/oauth2/v2.0/token",
        "/contoso/oauth2/v2.0/token",
    ]
    assert len(cache) == 3
    assert not any("rotated-secret" in part for key in cache for part in key)


@pytest.mark.usefixtures("no_default_tenant")
async def test_a_refused_client_credentials_exchange_is_not_cached(monkeypatch, app_log, caplog):
    caplog.set_level(logging.DEBUG)
    wire = TeamsWire()
    wire.login_status = 401
    wire.refusal_text = CANARY
    cache = _provider(monkeypatch, wire)
    config = channel_config("microsoft_teams")

    assert await teams._get_teams_access_token(config) is None
    assert cache == {}
    wire.login_status = 200
    assert await teams._get_teams_access_token(config) == "token-2"
    assert len(wire.logins) == 2
    assert CANARY not in _app_output(app_log) + caplog.text


@final
class Azure:
    """Stand-in for ``azure.identity.aio``: its credentials park in ``get_token`` while ``gate`` is closed."""

    def __init__(self, token: object, expires_on: object, failure: Exception | None) -> None:
        self.token: object = token
        self.expires_on: object = expires_on
        self.failure: Exception | None = failure
        self.credentials: list[AzureCredential] = []
        self.gate: asyncio.Event = asyncio.Event()
        self.gate.set()
        self.requested: asyncio.Event = asyncio.Event()

    def default_azure_credential(self) -> AzureCredential:
        self.credentials.append(AzureCredential(self))
        return self.credentials[-1]


@final
class AzureCredential:
    """One ``DefaultAzureCredential``, counting how often it was closed."""

    def __init__(self, azure: Azure) -> None:
        self.azure: Azure = azure
        self.scopes: list[str] = []
        self.closed: int = 0

    async def get_token(self, scope: str) -> SimpleNamespace:
        self.scopes.append(scope)
        self.azure.requested.set()
        await self.azure.gate.wait()
        if self.azure.failure is not None:
            raise self.azure.failure
        return SimpleNamespace(token=self.azure.token, expires_on=self.azure.expires_on)

    async def close(self) -> None:
        self.closed += 1


def _azure(
    monkeypatch: pytest.MonkeyPatch,
    *,
    issued: object = "mi-token",
    expires_on: object = 4600.0,
    failure: Exception | None = None,
) -> Azure:
    """Install the stand-in at a frozen clock of 1000 seconds, so ``expires_on`` 4600 is a one hour token."""
    azure = Azure(issued, expires_on, failure)
    monkeypatch.setitem(
        sys.modules, "azure.identity.aio", SimpleNamespace(DefaultAzureCredential=azure.default_azure_credential)
    )
    monkeypatch.setattr(teams, "time", SimpleNamespace(time=lambda: 1000.0))
    monkeypatch.delenv("TEAMS_TENANT_ID", raising=False)
    return azure


def _managed_identity() -> ChannelConfigRecord:
    config = channel_config("microsoft_teams")
    config.extra_config = {"use_managed_identity": True}
    return config


async def test_managed_identity_callers_share_one_credential_that_is_closed_and_cached_until_expiry(monkeypatch):
    cache = install_token_cache(monkeypatch)
    azure = _azure(monkeypatch)
    azure.gate.clear()
    config = _managed_identity()
    callers = [asyncio.create_task(teams._get_teams_access_token(config)) for _ in range(3)]
    async with asyncio.timeout(5):
        await azure.requested.wait()
    azure.gate.set()

    assert await asyncio.gather(*callers) == ["mi-token"] * 3
    assert await teams._get_teams_access_token(config) == "mi-token"

    assert len(azure.credentials) == 1
    assert azure.credentials[0].scopes == ["https://api.botframework.com/.default"]
    assert azure.credentials[0].closed == 1
    # No secret to hash: the entry is bound to the app and tenant, and lives until the token's own expiry less the skew.
    assert cache == {("teams_managed_identity", "bot-app", "common"): "mi-token"}
    assert cache.ttls == {("teams_managed_identity", "bot-app", "common"): 3300}


@pytest.mark.parametrize(
    ("failure", "issued", "expected"),
    [
        pytest.param(RuntimeError(CANARY), "mi-token", "kind=identity error_type=RuntimeError", id="identity-error"),
        pytest.param(None, 1234, "kind=malformed_response", id="malformed-token"),
    ],
)
async def test_managed_identity_failure_closes_the_credential_and_caches_nothing(
    monkeypatch, app_log, caplog, failure, issued, expected
):
    caplog.set_level(logging.DEBUG)
    cache = install_token_cache(monkeypatch)
    azure = _azure(monkeypatch, issued=issued, failure=failure)

    assert await teams._get_teams_access_token(_managed_identity()) is None

    assert cache == {}
    assert [credential.closed for credential in azure.credentials] == [1]
    output = _app_output(app_log)
    assert f"Teams: managed identity token request failed agent={AGENT_ID} {expected}" in output
    assert CANARY not in output + caplog.text


async def test_cancelling_the_managed_identity_mint_still_closes_the_credential(monkeypatch):
    azure = _azure(monkeypatch)
    azure.gate.clear()
    mint = asyncio.create_task(teams._mint_managed_identity_token(str(AGENT_ID)))
    async with asyncio.timeout(5):
        await azure.requested.wait()

    _ = mint.cancel()
    with pytest.raises(asyncio.CancelledError):
        await mint

    assert [credential.closed for credential in azure.credentials] == [1]


async def test_a_missing_azure_identity_package_returns_no_token(monkeypatch, app_log):
    cache = install_token_cache(monkeypatch)
    monkeypatch.setitem(sys.modules, "azure.identity.aio", None)

    assert await teams._get_teams_access_token(_managed_identity()) is None

    assert cache == {}
    assert "azure-identity package not installed" in _app_output(app_log)


def _bot_send(config: ChannelConfigRecord, *, text: str = "hello", strict: bool = True):
    config.extra_config = {**config.extra_config, "service_url": SERVICE_URL}
    return teams._send_teams_message(config, CONVERSATION, {"type": "message", "text": text}, strict=strict)


@pytest.mark.usefixtures("no_default_tenant")
async def test_a_rejected_token_is_dropped_and_the_activity_retried_once_with_a_fresh_one(monkeypatch):
    wire = TeamsWire()
    wire.replies = [
        httpx.Response(401, json={"error": {"code": "Unauthorized"}}),
        httpx.Response(200, json={"id": "a"}),
    ]
    cache = _provider(monkeypatch, wire)

    await _bot_send(channel_config("microsoft_teams"))

    assert wire.authorizations == ["Bearer token-1", "Bearer token-2"]
    assert len(wire.logins) == 2
    key, token = _sole_entry(cache)
    assert token == "token-2"
    assert cache.rejected == [(key, "token-1")]


@pytest.mark.usefixtures("no_default_tenant")
async def test_a_second_rejection_is_reported_after_exactly_one_retry(monkeypatch):
    wire = TeamsWire()
    wire.replies = [httpx.Response(401, json={"error": {"code": "Unauthorized"}})]
    _ = _provider(monkeypatch, wire)

    with pytest.raises(httpx.HTTPStatusError):
        await _bot_send(channel_config("microsoft_teams"))

    assert wire.authorizations == ["Bearer token-1", "Bearer token-2"]


@pytest.mark.usefixtures("no_default_tenant")
async def test_other_failures_are_not_retried_and_keep_the_cached_token(monkeypatch):
    wire = TeamsWire()
    wire.replies = [httpx.Response(403, json={"error": {"code": "BotNotInConversationRoster"}})]
    cache = _provider(monkeypatch, wire)

    with pytest.raises(httpx.HTTPStatusError):
        await _bot_send(channel_config("microsoft_teams"))

    assert wire.authorizations == ["Bearer token-1"]
    assert cache.rejected == []
    assert _sole_entry(cache)[1] == "token-1"


@pytest.mark.usefixtures("no_default_tenant")
async def test_later_chunks_of_a_long_reply_reuse_the_renewed_token(monkeypatch):
    wire = TeamsWire()
    wire.replies = [
        httpx.Response(401, json={"error": {"code": "Unauthorized"}}),
        httpx.Response(200, json={"id": "a"}),
    ]
    _ = _provider(monkeypatch, wire)

    await _bot_send(channel_config("microsoft_teams"), text="x" * (teams.TEAMS_MSG_LIMIT + 1))

    assert wire.authorizations == ["Bearer token-1", "Bearer token-2", "Bearer token-2"]
    assert len(wire.logins) == 2
