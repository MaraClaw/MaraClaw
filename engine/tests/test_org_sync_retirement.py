import hashlib
import hmac
from datetime import UTC, datetime
from importlib import import_module
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.transaction_pool_fakes import CountingPool

from app.api import enterprise, google_workspace
from app.config import get_settings
from app.dao.identity_provider_dao import identity_provider_dao
from app.dao.user_dao import user_dao
from app.db import session
from app.records.identity import IdentityProviderRecord
from app.services.auth_provider import GoogleWorkspaceAuthProvider
from app.services.google_workspace_oauth import parse_google_oauth_state


@pytest.fixture(params=[1, 3], ids=["provider-only", "actor-bound"])
def retired_state(request: pytest.FixtureRequest) -> str:
    payload = ":".join(["google_sync", *(str(uuid4()) for _ in range(request.param))])
    signature = hmac.new(get_settings().SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{signature}"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/enterprise/org/departments"),
        ("GET", "/enterprise/org/members"),
        ("POST", "/enterprise/org/sync"),
        ("GET", f"/enterprise/org/wecom-verify/{uuid4()}"),
        ("GET", "/enterprise/org/wecom-callback/retired-token"),
        ("GET", f"/enterprise/identity-providers/{uuid4()}/google-workspace-sync/authorize-url"),
    ],
)
def test_sync_routes_are_not_exposed(method: str, path: str, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = IdentityProviderRecord(uuid4(), "wecom", "WeCom")
    monkeypatch.setattr(identity_provider_dao, "get", AsyncMock(return_value=provider))
    app = FastAPI()
    app.include_router(enterprise.router, prefix="/api")
    app.include_router(google_workspace.router, prefix="/api")

    with TestClient(app) as client:
        response = client.request(method, f"/api{path}")

    assert response.status_code == 404


def test_signed_sync_state_is_rejected(retired_state: str) -> None:
    parsed = parse_google_oauth_state(retired_state)

    assert parsed is None


def test_sync_callback_stops_before_exchange_or_persistence(
    retired_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = CountingPool()
    monkeypatch.setattr(session, "get_pool", lambda: pool)
    actor = AsyncMock(return_value=None)
    provider = AsyncMock(return_value=None)
    exchange = AsyncMock(return_value={})
    persist = AsyncMock()
    monkeypatch.setattr(user_dao, "get_with_identity", actor)
    monkeypatch.setattr(identity_provider_dao, "get", provider)
    monkeypatch.setattr(identity_provider_dao, "update", persist)
    monkeypatch.setattr(GoogleWorkspaceAuthProvider, "exchange_code_for_token", exchange)
    app = FastAPI()
    app.include_router(google_workspace.router, prefix="/api")

    with TestClient(app) as client:
        response = client.get(
            "/api/auth/google_workspace/callback", params={"code": "unused-code", "state": retired_state}
        )

    assert response.status_code == 200
    assert "failed" in response.text.lower()
    actor.assert_not_awaited()
    provider.assert_not_awaited()
    exchange.assert_not_awaited()
    persist.assert_not_awaited()
    assert pool.checkouts == 0


@pytest.mark.parametrize(
    "module_name",
    ["app.services.org_sync.base", "app.services.org_sync_service", "app.services.org_sync_adapter"],
)
def test_sync_modules_are_not_importable(module_name: str) -> None:
    with pytest.raises(ModuleNotFoundError) as error:
        import_module(module_name)

    assert error.value.name in {module_name, "app.services.org_sync"}


def test_provider_response_omits_sync_status_and_redacts_stored_admin_secrets() -> None:
    provider = IdentityProviderRecord(
        uuid4(),
        "google_workspace",
        "Google SSO",
        created_at=datetime.now(UTC),
        config={
            "client_id": "ordinary-client",
            "client_secret": "ordinary-secret",
            "last_synced_at": "2026-10-01T00:00:00Z",
            "google_admin_refresh_token": "obsolete-plaintext-secret",
            "google_admin_refresh_token_encrypted": "obsolete-encrypted-secret",
        },
    )

    response = enterprise._identity_provider_response(provider)

    assert "last_synced_at" not in response
    config = response["config"]
    assert isinstance(config, dict)
    assert config["client_id"] == "ordinary-client"
    assert config["client_secret"] == "ordinary-secret"
    assert "google_admin_refresh_token" not in config
    assert "google_admin_refresh_token_encrypted" not in config
    assert provider.config["google_admin_refresh_token"] == "obsolete-plaintext-secret"


async def test_google_sso_authorization_uses_identity_scopes_without_admin_consent() -> None:
    provider = GoogleWorkspaceAuthProvider(config={"client_id": "ordinary-client", "client_secret": "secret"})
    redirect_uri = "https://example.test/api/auth/google_workspace/callback"

    url = await provider.get_authorization_url(redirect_uri, "sso-session-state")

    query = parse_qs(urlsplit(url).query)
    assert query["scope"] == ["openid email profile"]
    assert query["access_type"] == ["online"]
    assert query["prompt"] == ["select_account"]
    assert query["state"] == ["sso-session-state"]
    assert query["redirect_uri"] == [redirect_uri]
