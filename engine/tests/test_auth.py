"""Unit tests for the authentication API (app/api/auth.py)."""

import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Annotated, Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from app.api import auth as auth_api
from app.core import security
from app.core.json_types import json_object_from_response, object_mapping_from
from app.core.security import create_access_token, hash_password
from app.records.identity import IdentityRecord
from app.records.user import UserRecord
from app.schemas.schemas import UserLogin
from app.services import chat_tickets, password_reset_service

DEFAULT_TEST_CREDENTIAL = "correctpassword"


@pytest.mark.parametrize("flow", ["change", "reset"])
@pytest.mark.parametrize("forced", [False, True])
async def test_password_write_revokes_old_bearers_and_replaces_only_active_caller(
    monkeypatch: pytest.MonkeyPatch, flow: str, forced: bool
) -> None:
    # Given two admitted memberships sharing an identity, including a cached principal.
    identity = IdentityRecord(id=uuid.uuid4(), password_hash=hash_password("old-password"), must_change_password=forced)
    user = UserRecord(
        id=uuid.uuid4(), identity_id=identity.id, identity=identity, created_at=datetime(2026, 10, 25, tzinfo=UTC)
    )
    other = replace(user, id=uuid.uuid4())
    generation = [7]

    async def get_user(user_id: uuid.UUID, *, fresh: bool = False) -> UserRecord:
        return user if user_id == user.id else other

    async def get_generation(identity_id: uuid.UUID) -> int:
        assert identity_id == identity.id
        return generation[0]

    async def bump(identity_id: uuid.UUID) -> int:
        assert identity_id == identity.id
        generation[0] += 1
        return generation[0]

    async def update(*, db_obj: IdentityRecord, obj_in: dict[str, object]) -> IdentityRecord:
        for key, value in obj_in.items():
            setattr(db_obj, key, value)
        return db_obj

    monkeypatch.setattr(security.user_dao, "get_with_identity", get_user)
    monkeypatch.setattr(security.identity_dao, "get", AsyncMock(return_value=identity))
    monkeypatch.setattr(security.identity_dao, "get_auth_generation", get_generation)
    monkeypatch.setattr(security.identity_dao, "bump_auth_generation", bump)
    monkeypatch.setattr(security.identity_dao, "update", update)
    monkeypatch.setattr(
        password_reset_service, "consume_password_reset_token", AsyncMock(return_value={"identity_id": identity.id})
    )
    tokens = [
        await create_access_token(str(member.id), member.role, identity_id=identity.id) for member in (user, other)
    ]
    app = FastAPI()
    app.include_router(auth_api.router, prefix="/api")

    @app.get("/privileged")
    async def privileged(current_user: Annotated[UserRecord, Depends(security.get_current_user)]) -> dict[str, str]:
        return {"id": str(current_user.id)}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {tokens[0]}"}
        assert (await client.get("/api/auth/me", headers=headers)).status_code == 200
        assert (await client.get("/privileged", headers=headers)).status_code == (403 if forced else 200)
        # When either password writer succeeds.
        response = await client.request(
            "PUT" if flow == "change" else "POST",
            "/api/auth/me/password" if flow == "change" else "/api/auth/reset-password",
            json={"old_password": "old-password", "new_password": "new-password", "token": "r" * 32},
            headers=headers if flow == "change" else {},
        )
        # Then neither old bearer can survive, even with the unchanged cached membership.
        assert response.status_code == 200
        for token in tokens:
            assert (await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})).status_code == 401
        body = json_object_from_response(response)
        if flow == "reset":
            assert body == {"ok": True}
        else:
            assert body["ok"] is True
            assert body["must_change_password"] is False
            replacement = body["access_token"]
            assert isinstance(replacement, str)
            assert security.decode_access_token(replacement)["gen"] == 8
            headers = {"Authorization": f"Bearer {replacement}"}
            assert (await client.get("/api/auth/me", headers=headers)).status_code == 200
            assert (await client.get("/privileged", headers=headers)).status_code == 200


@pytest.mark.asyncio
async def test_bearer_rejects_revoked_generation_with_cached_principal(monkeypatch):
    # Given a token shared by memberships of one identity and a cached principal.
    identity = IdentityRecord(id=uuid.uuid4())
    user = UserRecord(id=uuid.uuid4(), identity_id=identity.id, identity=identity)
    monkeypatch.setattr(security.user_dao, "get", AsyncMock(return_value=user))
    monkeypatch.setattr(security.user_dao, "get_with_identity", AsyncMock(return_value=user))
    generation = AsyncMock(return_value=7)
    monkeypatch.setattr(security.identity_dao, "get_auth_generation", generation)
    token = await security.create_access_token(str(user.id), user.role)
    generation.return_value = 8

    # When the old credential is used, then fresh SQL wins over the cached user.
    with pytest.raises(HTTPException) as caught:
        await security.load_user_from_access_token(token)
    assert caught.value.status_code == 401
    assert generation.await_count == 2
    assert await security.load_user_principal(user.id) is user


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["membership", "identity", "generation"])
async def test_mint_fails_closed_when_current_identity_cannot_be_resolved(monkeypatch, missing):
    # Given no valid persisted identity generation for the membership.
    user = UserRecord(id=uuid.uuid4(), identity_id=None if missing == "identity" else uuid.uuid4())
    monkeypatch.setattr(security.user_dao, "get", AsyncMock(return_value=None if missing == "membership" else user))
    monkeypatch.setattr(security.identity_dao, "get_auth_generation", AsyncMock(return_value=None))

    # When minting, then no credential is issued from a snapshot or default generation.
    with pytest.raises(HTTPException) as caught:
        await security.create_access_token(str(user.id), user.role)
    assert caught.value.status_code == 401


@pytest.mark.asyncio
async def test_identity_only_mint_reads_persisted_generation_without_membership(monkeypatch):
    # Given an email-verification identity with no membership yet.
    identity_id = uuid.uuid4()
    membership = AsyncMock(side_effect=AssertionError("identity-only subject must not resolve membership"))
    monkeypatch.setattr(security.user_dao, "get", membership)
    generation = AsyncMock(return_value=9)
    monkeypatch.setattr(security.identity_dao, "get_auth_generation", generation)

    # When minting explicitly for that identity, then the persisted generation is retained.
    token = await security.create_access_token(str(identity_id), "user", identity_id=identity_id)
    assert security.decode_access_token(token)["gen"] == 9
    generation.assert_awaited_once_with(identity_id)


@pytest.mark.asyncio
async def test_chat_ticket_bearer_contract(monkeypatch):
    user = UserRecord(id=uuid.uuid4(), tenant_id=uuid.uuid4(), identity_id=uuid.uuid4())
    agent_id, session_id = uuid.uuid4(), uuid.uuid4()
    monkeypatch.setattr(security.user_dao, "get", AsyncMock(return_value=user))
    monkeypatch.setattr(security.identity_dao, "get_auth_generation", AsyncMock(return_value=7))
    token = await create_access_token(str(user.id), "member")
    monkeypatch.setattr(auth_api, "load_user_from_access_token", AsyncMock(return_value=user))
    monkeypatch.setattr(chat_tickets, "authorize", AsyncMock())
    monkeypatch.setattr(
        chat_tickets, "check_agent_access", AsyncMock(return_value=(SimpleNamespace(is_expired=False), "use"))
    )
    monkeypatch.setattr(chat_tickets, "resolve_session", AsyncMock(return_value=session_id))
    issue = AsyncMock(return_value=chat_tickets.ChatTicketResponse(ticket="opaque", session_id=session_id))
    monkeypatch.setattr(chat_tickets, "issue", issue)
    app = FastAPI()
    app.include_router(auth_api.router, prefix="/api")
    with TestClient(app) as client:
        response = client.post(
            "/api/auth/chat-ticket", json={"agent_id": str(agent_id)}, headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
        assert response.json() == {"ticket": "opaque", "session_id": str(session_id)}
        assert response.headers["cache-control"] == "no-store"
        assert client.post("/api/auth/chat-ticket", json={"agent_id": str(agent_id)}).status_code in (401, 403)
    assert issue.await_args is not None
    binding = issue.await_args.args[0]
    assert binding.user_id == user.id
    assert binding.tenant_id == user.tenant_id
    assert binding.agent_id == agent_id
    assert binding.session_id == session_id
    assert binding.auth_deadline == auth_api.decode_access_token(token)["exp"]
    assert binding.auth_generation == 7


def _dummy_request() -> Request:
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 8000),
        }
    )


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
    password=DEFAULT_TEST_CREDENTIAL,
    is_active=True,
    email_verified=True,
):
    """Create a fake Identity object with hashed password."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        email=email,
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
    return UserLogin(
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
        await auth_api.login(data, bg, _dummy_request())
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
        await auth_api.login(data, bg, _dummy_request())
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
        await auth_api.login(data, bg, _dummy_request())
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
        await auth_api.login(data, bg, _dummy_request())
    assert exc.value.status_code == 403
    detail = object_mapping_from(exc.value.detail)
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
        password_hash=hash_password(DEFAULT_TEST_CREDENTIAL),
    )
    user: Any = SimpleNamespace(
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

    with patch("app.api.auth.AuthUser", new=DummyUserOut):
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
    provider.provider = None
    user.identity = identity
    user.tenant_id = None
    provider.exchange_code_for_token = AsyncMock(return_value={"access_token": "provider-token"})
    provider.get_user_info = AsyncMock(return_value=SimpleNamespace())
    provider.find_or_create_user = AsyncMock(return_value=(user, False))
    data: Any = SimpleNamespace(
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
        patch("app.api.auth.AuthUser") as mock_user_out,
        patch.object(auth_api, "create_access_token", return_value="jwt-token"),
        patch.object(auth_api.user_dao, "get_by_identity_id", new=AsyncMock(return_value=[user])),
        patch.object(auth_api.user_dao, "get_with_identity", new=AsyncMock(return_value=user)),
        patch.object(auth_api.identity_provider_dao, "get_preferred", new=AsyncMock(return_value=None)),
    ):
        mock_user_out.model_validate.return_value = {"id": str(user.id)}
        result = await auth_api.oauth_callback("google", data)

    provider.exchange_code_for_token.assert_awaited_once_with("oauth-code", "https://example.com/oauth/callback/google")
    assert isinstance(result, DummyTokenResponse)
    assert result.access_token == "jwt-token"


@pytest.mark.asyncio
async def test_register_init_returns_submitted_email(monkeypatch):
    """Canonical registration echoes submitted email even when user.email is missing."""
    from app.schemas.schemas import RegisterInitRequest

    # Given a fresh identity and a membership without a stored email.
    data = RegisterInitRequest(email="ada@example.com", password="secret1")
    identity = SimpleNamespace(id=uuid.uuid4(), email=None, email_verified=True)
    user = SimpleNamespace(
        id=uuid.uuid4(),
        identity_id=identity.id,
        email=None,
        display_name="ada",
        avatar_url=None,
        role="member",
        is_platform_admin=False,
        tenant_id=None,
        title=None,
        primary_mobile=None,
        registration_source="web",
        is_active=True,
        email_verified=True,
        must_change_password=False,
        created_at=datetime.now(UTC),
        identity=None,
    )
    placement = SimpleNamespace(tenant_id=None, needs_org_confirm=False, suggested=None)

    @asynccontextmanager
    async def connection():
        yield

    monkeypatch.setattr(auth_api, "connection_ctx", connection)
    monkeypatch.setattr(auth_api.user_dao, "get_by_identity_id", AsyncMock(return_value=[]))
    monkeypatch.setattr(auth_api, "hash_password_async", AsyncMock(return_value="hashed"))
    monkeypatch.setattr(auth_api.identity_dao, "get_by_email", AsyncMock(return_value=None))
    monkeypatch.setattr(auth_api, "create_access_token", AsyncMock(return_value="tok"))

    with (
        patch("app.services.system_email_service.resolve_email_config_async", AsyncMock(return_value=None)),
        patch("app.services.org_membership.place_new_registration", AsyncMock(return_value=placement)),
        patch(
            "app.services.registration_service.registration_service.find_or_create_identity",
            AsyncMock(return_value=identity),
        ),
        patch(
            "app.services.registration_service.registration_service.create_user_with_identity",
            AsyncMock(return_value=user),
        ),
    ):
        # When registration runs through the current public handler.
        result = await auth_api.register_init(data, AsyncMock(), _dummy_request())

    # Then the response retains the submitted email and issued credentials.
    assert result.email == "ada@example.com"
    assert result.user_id == user.id
    assert result.access_token == "tok"
    assert "needs_company_setup" not in result.model_dump()


def test_http_header_reads_present_value_and_default():
    from app.core.json_types import http_header

    assert http_header({"content-type": "application/json"}, "content-type") == "application/json"
    assert http_header({}, "content-type", "image/png") == "image/png"
