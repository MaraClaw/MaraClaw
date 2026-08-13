import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.records.identity import AuthProviderType
from app.services.auth_provider import FeishuAuthProvider
from app.services.auth_registry import AuthProviderRegistry
from app.services.google_workspace_oauth import (
    GOOGLE_SSO_STATE_KIND,
    GOOGLE_SYNC_STATE_KIND,
    parse_google_oauth_state,
    sign_google_oauth_state,
    sign_google_sso_state,
)
from app.services.identity_provider_lookup import get_preferred_identity_provider


class _DummyResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _DummyAsyncClient:
    def __init__(self, responses):
        self._responses = list(responses)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, *args, **kwargs):
        return self._responses.pop(0)


class _DummyResult:
    def __init__(self, values):
        self._values = list(values)

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


@pytest.mark.asyncio
async def test_feishu_auth_provider_get_user_info():
    provider = FeishuAuthProvider(config={"app_id": "app-id", "app_secret": "app-secret"})

    responses = [
        _DummyResponse(
            {
                "data": {
                    "open_id": "ou_open_123",
                    "union_id": "on_union_456",
                    "name": "Alice",
                    "email": "alice@example.com",
                    "mobile": "13800000000",
                }
            }
        ),
    ]

    with (
        patch("app.services.auth_provider.httpx.AsyncClient", return_value=_DummyAsyncClient(responses)),
        patch.object(provider, "get_app_access_token", AsyncMock(return_value="app-token")),
    ):
        user_info = await provider.get_user_info("user-token")

    assert user_info.provider_user_id is None
    assert user_info.provider_union_id == "on_union_456"
    assert user_info.name == "Alice"
    assert user_info.email == "alice@example.com"
    assert user_info.mobile == "13800000000"


@pytest.mark.asyncio
async def test_identity_provider_lookup_tolerates_duplicate_rows(monkeypatch):
    from app.dao import identity_provider_dao
    from app.records.identity import IdentityProviderRecord

    tenant_id = uuid.uuid4()
    newer = IdentityProviderRecord(
        id=uuid.uuid4(),
        provider_type=AuthProviderType.GOOGLE_WORKSPACE,
        name="Newer Google Workspace",
        tenant_id=tenant_id,
        is_active=True,
        config={"client_id": "new"},
        updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
    )

    async def preferred(provider_type, tenant_id_arg=None, *, is_active=True):
        assert provider_type == "google_workspace"
        assert str(tenant_id_arg) == str(tenant_id)
        assert is_active is True
        return newer

    monkeypatch.setattr(identity_provider_dao, "get_preferred", preferred)

    provider = await get_preferred_identity_provider(
        "google_workspace",
        str(tenant_id),
        is_active=True,
    )

    assert provider is newer


@pytest.mark.asyncio
async def test_auth_registry_uses_preferred_provider_when_duplicates_exist(monkeypatch):
    from app.dao import identity_provider_dao
    from app.records.identity import IdentityProviderRecord

    tenant_id = uuid.uuid4()
    provider = IdentityProviderRecord(
        id=uuid.uuid4(),
        provider_type=AuthProviderType.GOOGLE_WORKSPACE,
        name="Google Workspace",
        tenant_id=tenant_id,
        is_active=True,
        config={"client_id": "client-id", "client_secret": "secret"},
        updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
    )

    async def preferred(provider_type, tenant_id_arg=None, *, is_active=True):
        assert provider_type == "google_workspace"
        assert str(tenant_id_arg) == str(tenant_id)
        assert is_active is True
        return provider

    monkeypatch.setattr(identity_provider_dao, "get_preferred", preferred)
    registry = AuthProviderRegistry()
    result = await registry.get_provider("google_workspace", str(tenant_id))

    assert result is not None
    assert result.provider is provider


def test_google_workspace_sso_state_includes_provider_id():
    sid = uuid.uuid4()
    provider_id = uuid.uuid4()

    state = sign_google_sso_state(sid, provider_id)
    parsed = parse_google_oauth_state(state)

    assert parsed == (GOOGLE_SSO_STATE_KIND, (sid, provider_id))


def test_google_workspace_sync_state_still_parses_single_uuid():
    provider_id = uuid.uuid4()

    state = sign_google_oauth_state(GOOGLE_SYNC_STATE_KIND, provider_id)
    parsed = parse_google_oauth_state(state)

    assert parsed == (GOOGLE_SYNC_STATE_KIND, (provider_id,))


def test_google_workspace_legacy_sso_state_still_parses():
    sid = uuid.uuid4()

    state = sign_google_oauth_state(GOOGLE_SSO_STATE_KIND, sid)
    parsed = parse_google_oauth_state(state)

    assert parsed == (GOOGLE_SSO_STATE_KIND, (sid,))
