"""Unit tests for the authentication API (app/api/auth.py)."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.api import auth as auth_api
from app.core.security import hash_password

DEFAULT_TEST_CREDENTIAL = "correctpassword"


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


class DummyResult:
    def __init__(self, values=None, scalar_value=None):
        self._values = list(values or [])
        self._scalar_value = scalar_value

    def scalar_one_or_none(self):
        if self._values:
            return self._values[0]
        return self._scalar_value

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


class RecordingDB:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.added = []
        self.committed = False
        self.refreshed = []

    async def execute(self, _statement, _params=None):
        if not self.responses:
            return DummyResult()
        return self.responses.pop(0)

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.committed = True

    async def refresh(self, value):
        self.refreshed.append(value)

    async def flush(self):
        pass


def _make_identity(
    *,
    email="test@example.com",
    username="testuser",
    password=DEFAULT_TEST_CREDENTIAL,
    is_active=True,
    email_verified=True,
):
    """Create a fake Identity object with hashed password."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        email=email,
        username=username,
        phone=None,
        password_hash=hash_password(password),
        is_active=is_active,
        email_verified=email_verified,
    )


def _make_user(identity_id, *, role="member", tenant_id=None):
    """Create a fake User object."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        identity_id=identity_id,
        role=role,
        tenant_id=tenant_id or uuid.uuid4(),
        identity=_make_identity(),
        is_active=True,
    )


def _make_login_data(login_identifier="test@example.com", password=DEFAULT_TEST_CREDENTIAL):
    return SimpleNamespace(
        login_identifier=login_identifier,
        password=password,
        tenant_id=None,
    )


# ---------------------------------------------------------------------------
# Login tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_invalid_credentials_no_identity(monkeypatch):
    """Login with a nonexistent user returns 401."""
    from app.dao import identity_dao

    async def no_identity(_identifier: str):
        return None

    monkeypatch.setattr(identity_dao, "get_by_login_identifier", no_identity)
    data = _make_login_data(login_identifier="nobody@example.com", password="whatever")
    bg = AsyncMock()

    with pytest.raises(HTTPException) as exc:
        await auth_api.login(data, bg)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_login_invalid_credentials_wrong_password(monkeypatch):
    """Login with wrong password returns 401."""
    from app.dao import identity_dao

    identity = _make_identity(password="correctpassword")

    async def get_identity(_identifier: str):
        return identity

    monkeypatch.setattr(identity_dao, "get_by_login_identifier", get_identity)
    data = _make_login_data(password="wrongpassword")
    bg = AsyncMock()

    with pytest.raises(HTTPException) as exc:
        await auth_api.login(data, bg)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_login_disabled_account(monkeypatch):
    """Login with a disabled account returns 403."""
    from app.dao import identity_dao

    identity = _make_identity(is_active=False)

    async def get_identity(_identifier: str):
        return identity

    monkeypatch.setattr(identity_dao, "get_by_login_identifier", get_identity)
    data = _make_login_data()
    bg = AsyncMock()

    with pytest.raises(HTTPException) as exc:
        await auth_api.login(data, bg)
    assert exc.value.status_code == 403
    assert "disabled" in str(exc.value.detail).lower()


@pytest.mark.asyncio
async def test_login_unverified_email(monkeypatch):
    """Login with unverified email returns 403 with verification info."""
    from app.dao import identity_dao, user_dao

    identity = _make_identity(email_verified=False)
    user = _make_user(identity.id)

    async def get_identity(_identifier: str):
        return identity

    async def representative(_identity_id):
        return user

    monkeypatch.setattr(identity_dao, "get_by_login_identifier", get_identity)
    monkeypatch.setattr(user_dao, "get_representative_user_for_identity", representative)
    data = _make_login_data()
    bg = AsyncMock()

    with (
        patch(
            "app.services.system_email_service.resolve_email_config_async",
            new_callable=AsyncMock,
            return_value={"host": "localhost"},
        ),
        patch.object(auth_api, "_send_verification_email_task", new_callable=AsyncMock),
        pytest.raises(HTTPException) as exc,
    ):
        await auth_api.login(data, bg)
    assert exc.value.status_code == 403
    detail = exc.value.detail
    assert isinstance(detail, dict)
    assert detail["needs_verification"] is True


# ---------------------------------------------------------------------------
# /me tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_me_returns_user():
    """GET /me with an authenticated user returns user data."""
    identity = SimpleNamespace(
        id=uuid.uuid4(),
        email="test@example.com",
        username="testuser",
        password_hash=hash_password(DEFAULT_TEST_CREDENTIAL),
    )
    user = SimpleNamespace(
        id=uuid.uuid4(),
        identity_id=identity.id,
        identity=identity,
        role="member",
        tenant_id=uuid.uuid4(),
        display_name="Test User",
        avatar_url=None,
        email=identity.email,
        is_platform_admin=False,
    )

    class DummyUserOut:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

        @classmethod
        def model_validate(cls, obj):
            return cls(id=str(obj.id), email=obj.email)

    with patch("app.api.auth.UserOut", new=DummyUserOut):
        result = await auth_api.get_me(current_user=user)
    assert result.id == str(user.id)
    assert result.email == user.email
    assert result.is_platform_admin is False


@pytest.mark.asyncio
async def test_oauth_callback_passes_redirect_uri():
    """OAuth callback should forward redirect_uri for providers like Google."""
    identity = _make_identity()
    user = _make_user(identity.id)
    provider = AsyncMock()
    provider.exchange_code_for_token = AsyncMock(return_value={"access_token": "provider-token"})
    provider.get_user_info = AsyncMock(return_value=SimpleNamespace())
    provider.find_or_create_user = AsyncMock(return_value=(user, False))
    data = SimpleNamespace(
        code="oauth-code",
        state="oauth-state",
        redirect_uri="https://example.com/oauth/callback/google",
        pending_token=None,
        tenant_id=None,
    )

    class DummyTokenResponse:
        def __init__(self, access_token, **kwargs):
            self.access_token = access_token

    with (
        patch("app.services.auth_registry.auth_provider_registry.get_provider", new=AsyncMock(return_value=provider)),
        patch("app.api.auth.TokenResponse", new=DummyTokenResponse),
        patch("app.api.auth.UserOut") as mock_user_out,
        patch.object(auth_api, "create_access_token", return_value="jwt-token"),
        patch.object(auth_api.user_dao, "get_by_identity_id", new=AsyncMock(return_value=[user])),
    ):
        mock_user_out.model_validate.return_value = {"id": str(user.id)}
        result = await auth_api.oauth_callback("google", data)

    provider.exchange_code_for_token.assert_awaited_once_with("oauth-code", "https://example.com/oauth/callback/google")
    assert result.access_token == "jwt-token"
