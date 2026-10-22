from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.transaction_pool_fakes import CountingPool

from app.api import auth
from app.core.security import decode_access_token, hash_password, verify_password
from app.db import session
from app.records.identity import IdentityRecord
from app.records.user import UserRecord
from app.services import org_membership, system_email_service
from app.services.registration_service import participant_dao, registration_service


@pytest.fixture
def account(monkeypatch) -> UserRecord:
    now = datetime.now(UTC)
    identity = IdentityRecord(
        id=uuid4(),
        email="alice@example.com",
        phone="15551234567",
        password_hash=hash_password("password123"),
        email_verified=True,
        must_change_password=True,
        created_at=now,
        updated_at=now,
    )
    user = UserRecord(
        id=uuid4(),
        identity_id=identity.id,
        display_name="Alice",
        identity=identity,
        created_at=now,
    )
    monkeypatch.setattr(auth.user_dao, "get", AsyncMock(return_value=user))
    monkeypatch.setattr(auth.identity_dao, "get_auth_generation", AsyncMock(return_value=7))
    return user


@pytest.fixture
def http_app() -> FastAPI:
    app = FastAPI()
    app.include_router(auth.router, prefix="/api")
    return app


@pytest.mark.parametrize("identifier", ["alice", "+15551234567", "15551234567", "alice@", "", "  alice  "])
async def test_login_rejects_non_email_before_credentials(
    monkeypatch, http_app: FastAPI, account: UserRecord, identifier: str
):
    lookup = AsyncMock(return_value=account.identity)
    monkeypatch.setattr(auth.identity_dao, "get_by_login_identifier", lookup)

    async with AsyncClient(transport=ASGITransport(app=http_app), base_url="http://test") as client:
        response = await client.post(
            "/api/auth/login", json={"login_identifier": identifier, "password": "password123"}
        )

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "login_identifier"]
    lookup.assert_not_awaited()


@pytest.mark.parametrize(("password", "status"), [("password123", 200), ("incorrect", 401)])
async def test_login_accepts_trimmed_email_and_checks_password(
    monkeypatch,
    http_app: FastAPI,
    account: UserRecord,
    password: str,
    status: int,
):
    lookup = AsyncMock(return_value=account.identity)
    monkeypatch.setattr(auth.identity_dao, "get_by_login_identifier", lookup)
    monkeypatch.setattr(auth.user_dao, "get_by_identity_id", AsyncMock(return_value=[account]))
    monkeypatch.setattr(auth, "_pending_org_fields", AsyncMock(return_value=(False, None)))

    async with AsyncClient(transport=ASGITransport(app=http_app), base_url="http://test") as client:
        response = await client.post(
            "/api/auth/login",
            json={"login_identifier": "  ALIce@EXAMPLE.COM  ", "password": password},
        )

    assert response.status_code == status
    lookup.assert_awaited_once_with("ALIce@example.com")
    if status == 200:
        body = response.json()
        assert decode_access_token(body["access_token"])["sub"] == str(account.id)
        assert body["identity"]["id"] == str(account.identity_id)
        assert body["must_change_password"] is True
        assert "username" not in body["identity"]
        assert "username" not in body["user"]


@pytest.mark.parametrize("display_name", [None, "Alice Cooper"])
async def test_registration_needs_only_email_and_password(
    monkeypatch,
    http_app: FastAPI,
    account: UserRecord,
    display_name: str | None,
):
    pool = CountingPool()
    monkeypatch.setattr(session, "get_pool", lambda: pool)
    monkeypatch.setattr(system_email_service, "resolve_email_config_async", AsyncMock(return_value=None))
    monkeypatch.setattr("app.services.registration_service.resolve_email_config_async", AsyncMock(return_value=None))
    monkeypatch.setattr(auth.identity_dao, "get_by_email", AsyncMock(return_value=None))
    create_identity = AsyncMock(return_value=account.identity)
    monkeypatch.setattr(auth.identity_dao, "create_identity", create_identity)
    monkeypatch.setattr(auth.user_dao, "get_by_identity_id", AsyncMock(return_value=[]))
    account.display_name = display_name or "alice"
    create_user = AsyncMock(return_value=account)
    monkeypatch.setattr(auth.user_dao, "create", create_user)
    monkeypatch.setattr(registration_service, "bind_org_member", AsyncMock())
    monkeypatch.setattr(participant_dao, "create_for_user", AsyncMock())
    monkeypatch.setattr(
        org_membership,
        "place_new_registration",
        AsyncMock(return_value=SimpleNamespace(tenant_id=None, suggested=None, needs_org_confirm=False)),
    )
    payload = {"email": "alice@example.com", "password": "password123"}
    if display_name is not None:
        payload["display_name"] = display_name

    async with AsyncClient(transport=ASGITransport(app=http_app), base_url="http://test") as client:
        response = await client.post("/api/auth/register/init", json=payload)

    assert response.status_code == 201
    assert response.json()["user"]["display_name"] == (display_name or "alice")
    assert create_user.await_args is not None
    assert create_user.await_args.kwargs["obj_in"]["display_name"] == (display_name or "alice")
    assert create_identity.await_args is not None
    created = create_identity.await_args.kwargs
    assert created["email"] == "alice@example.com"
    assert created["is_platform_admin"] is False
    assert verify_password("password123", created["password_hash"])
    assert "username" not in created
    assert "username" not in response.json()["user"]
    assert pool.raw.commits == 1


async def test_retired_profile_field_cannot_write_account_data(monkeypatch, http_app: FastAPI, account: UserRecord):
    pool = CountingPool()
    monkeypatch.setattr(session, "get_pool", lambda: pool)
    http_app.dependency_overrides[auth.get_current_user] = lambda: account
    monkeypatch.setattr(auth.user_dao, "get_with_identity", AsyncMock(return_value=account))
    update_identity, update_user = AsyncMock(), AsyncMock()
    monkeypatch.setattr(auth.identity_dao, "update", update_identity)
    monkeypatch.setattr(auth.user_dao, "update", update_user)

    async with AsyncClient(transport=ASGITransport(app=http_app), base_url="http://test") as client:
        response = await client.patch("/api/auth/me", json={"username": "legacy-handle"})

    assert response.status_code == 200
    assert response.json()["display_name"] == "Alice"
    assert "username" not in response.json()
    update_identity.assert_not_awaited()
    update_user.assert_not_awaited()


async def test_retired_availability_query_has_no_account_lookup(monkeypatch, http_app: FastAPI):
    lookup = AsyncMock()
    monkeypatch.setattr(auth.identity_dao, "get_by_email", lookup)

    async with AsyncClient(transport=ASGITransport(app=http_app), base_url="http://test") as client:
        response = await client.get("/api/auth/check-duplicate", params={"username": "legacy-handle"})

    assert response.status_code == 200
    assert response.json() == {"email_exists": False, "conflicts": [], "has_conflict": False}
    lookup.assert_not_awaited()


def test_public_account_schemas_have_no_username(http_app: FastAPI):
    schemas = http_app.openapi()["components"]["schemas"]

    for name in ("RegisterInitRequest", "UserUpdate", "UserOut"):
        assert "username" not in schemas[name]["properties"]
    assert schemas["RegisterInitRequest"]["required"] == ["email", "password"]
