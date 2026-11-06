from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient, Response
from tests.oauth_state_fakes import REDIRECT_URI, StateRedis, install_state_redis
from tests.transaction_pool_fakes import CountingPool

from app.api import auth
from app.db import session
from app.services import oauth_state
from app.services.auth_audience import AuthAudience
from app.services.auth_registry import auth_provider_registry


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> StateRedis:
    return install_state_redis(monkeypatch)


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setattr(session, "get_pool", lambda: CountingPool())
    idp = SimpleNamespace(
        provider=None,
        config={},
        get_authorization_url=AsyncMock(side_effect=lambda redirect, state: f"https://idp.test/auth?state={state}"),
    )
    monkeypatch.setattr(auth_provider_registry, "get_provider", AsyncMock(return_value=idp))
    return idp


@pytest.fixture
def app() -> FastAPI:
    application = FastAPI()
    application.include_router(auth.router)
    return application


async def authorize(app: FastAPI, **params: str) -> Response:
    query = {"redirect_uri": REDIRECT_URI, **params}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get("/auth/google/authorize", params=query)


@pytest.mark.parametrize(
    ("params", "intent"),
    [
        ({}, oauth_state.OAuthIntent.SIGN_IN),
        ({"intent": "sign_in"}, oauth_state.OAuthIntent.SIGN_IN),
        ({"intent": "bind"}, oauth_state.OAuthIntent.BIND),
    ],
)
async def test_authorize_mints_a_server_state_bound_to_provider_redirect_and_intent(
    app: FastAPI, redis: StateRedis, provider: SimpleNamespace, params: dict[str, str], intent: oauth_state.OAuthIntent
) -> None:
    # Given a client that also tries to pick its own state.
    # When authorize is called.
    response = await authorize(app, state="client-picked", **params)
    # Then the server returns and uses its own state, and stores its binding with a short TTL.
    assert response.status_code == 200
    body = response.json()
    assert body["state"] != "client-picked"
    assert body["authorization_url"] == f"https://idp.test/auth?state={body['state']}"
    provider.get_authorization_url.assert_awaited_once_with(REDIRECT_URI, body["state"])
    ((key, raw),) = redis.rows.items()
    assert oauth_state.OAuthStateRecord.model_validate_json(raw) == oauth_state.OAuthStateRecord(
        provider="google", redirect_uri=REDIRECT_URI, intent=intent
    )
    assert redis.ttls[key] == oauth_state.STATE_TTL_SECONDS
    assert body["state"] not in key


async def test_authorize_issues_a_different_state_each_time(
    app: FastAPI, redis: StateRedis, provider: SimpleNamespace
) -> None:
    first = (await authorize(app)).json()["state"]
    second = (await authorize(app)).json()["state"]
    assert first != second
    assert len(redis.rows) == 2


async def test_authorize_fails_closed_when_state_cannot_be_stored(
    app: FastAPI, redis: StateRedis, provider: SimpleNamespace
) -> None:
    # Given Redis refuses writes.
    redis.fail_on.add("set")
    # When authorize is called.
    response = await authorize(app)
    # Then no authorization URL is produced.
    assert response.status_code == 503
    provider.get_authorization_url.assert_not_awaited()


async def test_authorize_for_an_unknown_provider_stores_nothing(
    app: FastAPI, redis: StateRedis, monkeypatch: pytest.MonkeyPatch, provider: SimpleNamespace
) -> None:
    monkeypatch.setattr(auth_provider_registry, "get_provider", AsyncMock(return_value=None))
    response = await authorize(app)
    assert response.status_code == 404
    assert redis.rows == {}


@pytest.mark.parametrize("redirect_uri", ["", "https://app.example.test/" + "a" * oauth_state.MAX_REDIRECT_URI_LENGTH])
async def test_authorize_bounds_the_redirect_uri_it_stores(
    app: FastAPI, redis: StateRedis, provider: SimpleNamespace, redirect_uri: str
) -> None:
    response = await authorize(app, redirect_uri=redirect_uri)
    assert response.status_code == 422
    assert redis.rows == {}


async def test_callback_bounds_the_state_an_unauthenticated_client_may_send(app: FastAPI, redis: StateRedis) -> None:
    payload = {"code": "code", "state": "s" * (oauth_state.MAX_STATE_LENGTH + 1)}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/auth/google/callback", json=payload)
    assert response.status_code == 422


def pending(audience: AuthAudience = AuthAudience.MEMBER) -> auth.OAuthPendingCache:
    return auth.OAuthPendingCache(
        provider_type="google",
        audience=audience,
        user_info=auth.OAuthPendingUserInfo(provider_type="google", provider_user_id="remote", raw_data={}),
        token_data={},
    )


async def test_pending_token_is_redeemable_exactly_once_with_its_audience(redis: StateRedis) -> None:
    # Given a cached tenant selection.
    await auth._cache_oauth_pending("token", pending(AuthAudience.MEMBER))
    # When it is redeemed twice.
    first = await auth._get_oauth_pending("token")
    second = await auth._get_oauth_pending("token")
    # Then only the first redemption sees it, and it records provider and audience.
    assert first is not None
    assert (first.provider_type, first.audience) == ("google", AuthAudience.MEMBER)
    assert second is None
    assert redis.ttls == {}


async def test_pending_token_is_stored_with_a_ten_minute_expiry(redis: StateRedis) -> None:
    await auth._cache_oauth_pending("token", pending())
    assert list(redis.ttls.values()) == [auth._OAUTH_PENDING_TTL]


async def test_pending_token_without_an_audience_is_unusable(redis: StateRedis) -> None:
    # Given an entry written before pending tokens recorded their audience.
    redis.rows["oauth_pending:legacy"] = pending().model_dump_json(exclude={"audience"})
    # When it is redeemed.
    assert await auth._get_oauth_pending("legacy") is None


async def test_pending_token_cannot_be_cached_when_storage_is_down(redis: StateRedis) -> None:
    redis.fail_on.add("set")
    with pytest.raises(HTTPException) as caught:
        await auth._cache_oauth_pending("token", pending())
    assert caught.value.status_code == 503


async def test_pending_token_cannot_be_redeemed_when_storage_is_down(redis: StateRedis) -> None:
    redis.fail_on.add("getdel")
    with pytest.raises(HTTPException) as caught:
        await auth._get_oauth_pending("token")
    assert caught.value.status_code == 503
