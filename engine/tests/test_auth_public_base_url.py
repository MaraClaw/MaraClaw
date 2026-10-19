from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request
from tests.transaction_pool_fakes import CountingPool

from app.api import auth
from app.config import Settings
from app.db import session
from app.main import app
from app.records.identity import IdentityRecord
from app.records.user import UserRecord
from app.services import (
    frontend_origin,
    org_membership,
    password_reset_service,
    platform_service as platform_module,
    system_email_service,
)
from app.services.email_verification_service import email_verification_service
from app.services.platform_service import PublicBaseUrlNotConfiguredError
from app.services.registration_service import registration_service


@pytest.fixture
def missing_public_base(monkeypatch):
    settings = Settings(PUBLIC_BASE_URL="", CORS_ORIGINS=["https://allowed.example"])
    monkeypatch.setattr(platform_module, "get_settings", lambda: settings)
    monkeypatch.setattr(frontend_origin, "get_settings", lambda: settings)
    pool = CountingPool()
    monkeypatch.setattr(session, "get_pool", lambda: pool)
    monkeypatch.setattr(system_email_service, "resolve_email_config_async", AsyncMock(return_value=object()))


@pytest.mark.asyncio
async def test_registration_succeeds_and_logs_skipped_verification_without_trusted_base(
    monkeypatch, missing_public_base
):
    identity = IdentityRecord(id=uuid4(), email="new@example.com", username="newuser")
    user = UserRecord(id=uuid4(), identity_id=identity.id, display_name="New User", created_at=datetime.now(UTC))
    monkeypatch.setattr(auth.identity_dao, "get_by_email", AsyncMock(return_value=None))
    monkeypatch.setattr(auth.user_dao, "get_by_identity_id", AsyncMock(return_value=[]))
    monkeypatch.setattr(registration_service, "find_or_create_identity", AsyncMock(return_value=identity))
    monkeypatch.setattr(registration_service, "create_user_with_identity", AsyncMock(return_value=user))
    monkeypatch.setattr(
        org_membership,
        "place_new_registration",
        AsyncMock(return_value=SimpleNamespace(tenant_id=None, suggested=None, needs_org_confirm=False)),
    )
    monkeypatch.setattr(auth, "hash_password_async", AsyncMock(return_value="hashed"))
    monkeypatch.setattr(auth, "create_access_token", lambda *_: "registration-token")
    create_verification = AsyncMock()
    monkeypatch.setattr(email_verification_service, "create_email_verification_token", create_verification)
    schedule = Mock()
    warning = Mock()
    monkeypatch.setattr(auth, "schedule_background_task", schedule)
    monkeypatch.setattr(auth.logger, "warning", warning)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://untrusted.example") as client:
        response = await client.post(
            "/api/auth/register/init",
            json={"email": identity.email, "username": identity.username, "password": "password123"},
            headers={"origin": "https://evil.example"},
        )

    assert response.status_code == 201
    assert response.json()["access_token"] == "registration-token"
    create_verification.assert_not_awaited()
    schedule.assert_not_called()
    assert any("PUBLIC_BASE_URL" in call.args[0] for call in warning.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("known_identity", [False, True])
async def test_forgot_password_stays_generic_without_public_base(monkeypatch, missing_public_base, known_identity):
    identity = IdentityRecord(id=uuid4(), email="alice@example.com", username="alice")
    lookup = AsyncMock(return_value=identity if known_identity else None)
    monkeypatch.setattr(auth.identity_dao, "get_by_email", lookup)
    create_token = AsyncMock(return_value=("reset-token", datetime.now(UTC) + timedelta(minutes=15)))
    monkeypatch.setattr(password_reset_service, "create_password_reset_token", create_token)
    schedule = Mock()
    monkeypatch.setattr(auth, "schedule_background_task", schedule)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://untrusted.example") as client:
        response = await client.post("/api/auth/forgot-password", json={"email": identity.email})

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "message": "If an account with that email exists, a password reset email has been sent.",
    }
    lookup.assert_awaited_once_with(identity.email)
    assert create_token.await_count == int(known_identity)
    schedule.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [[], [(b"origin", b"https://evil.example")], [(b"referer", b"https://evil.example/login")]],
)
async def test_frontend_base_rejects_untrusted_request_sources(missing_public_base, headers):
    request = Request({"type": "http", "scheme": "https", "server": ("untrusted.example", 443), "headers": headers})

    with pytest.raises(PublicBaseUrlNotConfiguredError):
        await frontend_origin.resolve_frontend_base_url(request)


@pytest.mark.asyncio
@pytest.mark.parametrize("header", ["origin", "referer"])
async def test_allowlisted_frontend_works_without_public_base(missing_public_base, header):
    value = "https://allowed.example" if header == "origin" else "https://allowed.example/login"
    request = Request({"type": "http", "headers": [(header.encode(), value.encode())]})

    url = await password_reset_service.build_password_reset_url("reset-token", request)

    assert url == "https://allowed.example/reset-password?token=reset-token"
