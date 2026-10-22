from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import TypeAdapter, ValidationError
from tests.transaction_pool_fakes import CountingPool

from app.api import auth
from app.core.json_types import json_object_from, json_object_from_response
from app.db import session
from app.records.identity import IdentityProviderRecord, IdentityRecord
from app.records.tenant import TenantRecord
from app.records.user import UserRecord
from app.schemas.auth_responses import (
    AuthProviderResponse,
    CheckDuplicateResponse,
    EmailConflict,
    ForgotPasswordResponse,
    RegistrationConfigResponse,
    ResendVerificationResponse,
    ResetPasswordResponse,
)
from app.schemas.schemas import MultiTenantResponse, TokenResponse
from app.services import password_reset_service, system_email_service
from app.services.auth_provider import ExternalUserInfo
from app.services.auth_registry import auth_provider_registry


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    pool = CountingPool()
    monkeypatch.setattr(session, "get_pool", lambda: pool)
    application = FastAPI()
    application.include_router(auth.router)
    return application


@pytest.mark.parametrize("path", ["/auth/login", "/auth/google/callback"])
@pytest.mark.parametrize("membership_count", [1, 2])
async def test_auth_response_union_preserves_payloads(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, path: str, membership_count: int
) -> None:
    timestamp = datetime(2026, 10, 21, tzinfo=UTC)
    identity = IdentityRecord(
        id=uuid4(),
        email="member@example.com",
        password_hash="stored-hash",
        email_verified=True,
        must_change_password=True,
        created_at=timestamp,
        updated_at=timestamp,
    )
    tenants = [TenantRecord(id=uuid4(), name=f"Company {index}", slug=f"company-{index}") for index in range(2)]
    users = [
        UserRecord(
            id=uuid4(),
            identity_id=identity.id,
            identity=identity,
            tenant_id=tenant.id,
            display_name="Member",
            created_at=timestamp,
        )
        for tenant in tenants[:membership_count]
    ]
    monkeypatch.setattr(auth.identity_dao, "get_by_login_identifier", AsyncMock(return_value=identity))
    monkeypatch.setattr(auth, "verify_password_async", AsyncMock(return_value=True))
    monkeypatch.setattr(auth.user_dao, "get_by_identity_id", AsyncMock(return_value=users))
    monkeypatch.setattr(auth.user_dao, "get_with_identity", AsyncMock(return_value=users[0]))
    monkeypatch.setattr(auth.user_dao, "get", AsyncMock(return_value=users[0]))
    monkeypatch.setattr(auth.identity_dao, "get_auth_generation", AsyncMock(return_value=7))
    monkeypatch.setattr(auth.tenant_dao, "get", AsyncMock(return_value=tenants[0]))
    monkeypatch.setattr(auth.tenant_dao, "get_by_ids", AsyncMock(return_value=tenants))
    monkeypatch.setattr(auth.identity_provider_dao, "get_preferred", AsyncMock(return_value=None))
    provider = SimpleNamespace(
        provider=None,
        config={},
        exchange_code_for_token=AsyncMock(return_value={"access_token": "remote"}),
        get_user_info=AsyncMock(return_value=ExternalUserInfo(provider_type="google", email="member@example.com")),
        find_or_create_user=AsyncMock(return_value=(users[0], False)),
    )
    monkeypatch.setattr(auth_provider_registry, "get_provider", AsyncMock(return_value=provider))
    monkeypatch.setattr(auth, "_cache_oauth_pending", AsyncMock())
    request = (
        {"login_identifier": " member@example.com ", "password": "password", "tenant_id": None, "unknown": True}
        if path == "/auth/login"
        else {"code": "code", "state": "state", "redirect_uri": None, "unknown": True}
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(path, json=request)

    assert response.status_code == 200
    body = json_object_from_response(response)
    if membership_count == 2:
        assert set(body) == {"requires_tenant_selection", "login_identifier", "tenants", "pending_token"}
        assert body["requires_tenant_selection"] is True
        assert body["login_identifier"] == "member@example.com"
        assert body["tenants"] == [
            {"tenant_id": str(tenant.id), "tenant_name": tenant.name, "tenant_slug": tenant.slug, "logo_url": None}
            for tenant in tenants
        ]
        if path == "/auth/login":
            assert body["pending_token"] is None
        else:
            assert isinstance(body["pending_token"], str)
            assert len(body["pending_token"]) == 32
    else:
        assert set(body) == {
            "access_token",
            "token_type",
            "user",
            "identity",
            "tenant_name",
            "must_change_password",
            "needs_org_confirm",
            "suggested_org",
        }
        assert isinstance(body["access_token"], str)
        assert body["access_token"]
        assert body["token_type"] == "bearer"
        assert body["tenant_name"] is None
        assert body["suggested_org"] is None
        assert body["needs_org_confirm"] is False
        user = json_object_from(body["user"])
        assert user["id"] == str(users[0].id)
        assert user["avatar_url"] is None
        assert user["primary_mobile"] is None
        assert user["must_change_password"] is True
        assert "password_hash" not in user
        if path == "/auth/login":
            assert body["must_change_password"] is True
            assert json_object_from(body["identity"])["must_change_password"] is True
        else:
            assert body["must_change_password"] is False
            assert body["identity"] is None


@pytest.mark.parametrize("path", ["/auth/login", "/auth/{provider}/callback"])
def test_openapi_documents_both_existing_auth_models(app: FastAPI, path: str) -> None:
    schema = json_object_from(app.openapi())

    for key in ("paths", path, "post", "responses", "200", "content", "application/json", "schema"):
        schema = json_object_from(schema[key])

    assert schema["anyOf"] == [
        {"$ref": "#/components/schemas/TokenResponse"},
        {"$ref": "#/components/schemas/MultiTenantResponse"},
    ]


@pytest.mark.parametrize("enabled", [True, False])
async def test_registration_config_keeps_exact_dictionary(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    monkeypatch.setattr(auth.system_setting_dao, "is_invitation_code_enabled", AsyncMock(return_value=enabled))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/auth/registration-config")

    assert response.status_code == 200
    assert json_object_from_response(response) == {"invitation_code_required": enabled}


@pytest.mark.parametrize("identity", [None, IdentityRecord(id=uuid4(), is_active=False)])
async def test_forgot_password_keeps_anti_enumeration_dictionary(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, identity: IdentityRecord | None
) -> None:
    monkeypatch.setattr(system_email_service, "resolve_email_config_async", AsyncMock(return_value=object()))
    monkeypatch.setattr(auth.identity_dao, "get_by_email", AsyncMock(return_value=identity))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/auth/forgot-password", json={"email": "member@example.com", "unknown": True})

    assert response.status_code == 200
    assert json_object_from_response(response) == {
        "ok": True,
        "message": "If an account with that email exists, a password reset email has been sent.",
    }


@pytest.mark.parametrize("valid_token", [True, False])
async def test_reset_password_keeps_success_and_error_contracts(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, valid_token: bool
) -> None:
    identity = IdentityRecord(id=uuid4())
    monkeypatch.setattr(
        password_reset_service,
        "consume_password_reset_token",
        AsyncMock(return_value={"identity_id": identity.id} if valid_token else None),
    )
    monkeypatch.setattr(auth.identity_dao, "get", AsyncMock(return_value=identity))
    monkeypatch.setattr(auth.identity_dao, "update", AsyncMock(return_value=identity))
    monkeypatch.setattr(auth, "hash_password_async", AsyncMock(return_value="new-hash"))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/auth/reset-password", json={"token": "a" * 20, "new_password": "new-password"})

    assert response.status_code == (200 if valid_token else 400)
    assert json_object_from_response(response) == (
        {"ok": True} if valid_token else {"detail": "Invalid or expired reset token"}
    )


@pytest.mark.parametrize("code_fields", [{}, {"code": None}, {"code": ""}])
async def test_oauth_missing_or_null_code_preserves_error(app: FastAPI, code_fields: dict[str, object]) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/auth/google/callback", json={"state": "state", **code_fields})

    assert response.status_code == 400
    assert json_object_from_response(response) == {"detail": "Missing authorization code"}


@pytest.mark.parametrize("email", [None, "", "member@example.com", "not-an-email"])
@pytest.mark.parametrize("registered", [False, True])
async def test_duplicate_check_keeps_global_email_conflict_dictionary(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, email: str | None, registered: bool
) -> None:
    lookup = AsyncMock(return_value=IdentityRecord(id=uuid4(), is_active=False) if registered else None)
    monkeypatch.setattr(auth.identity_dao, "get_by_email", lookup)
    exists = bool(email and registered)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/auth/check-duplicate", params={"email": email} if email is not None else {})

    assert response.status_code == 200
    assert json_object_from_response(response) == {
        "email_exists": exists,
        "conflicts": [{"type": "email", "scope": "global", "message": "Email already registered"}] if exists else [],
        "has_conflict": exists,
    }
    if bool(email):
        lookup.assert_awaited_once_with(email)
    else:
        lookup.assert_not_awaited()


@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("provider_count", [0, 2])
async def test_provider_list_keeps_public_projection_and_tenant_scope(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, scoped: bool, provider_count: int
) -> None:
    tenant_id = uuid4() if scoped else None
    providers = [
        IdentityProviderRecord(
            id=uuid4(),
            provider_type=f"custom-{index}",
            name="" if index else "Provider",
            is_active=bool(index),
            sso_login_enabled=True,
            config={"client_secret": "private", "access_token": "private"},
            tenant_id=tenant_id,
        )
        for index in range(provider_count)
    ]
    lookup = AsyncMock(return_value=providers)
    monkeypatch.setattr(auth_provider_registry, "list_providers", lookup)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/auth/providers", params={"tenant_id": str(tenant_id)} if scoped else {})

    assert response.status_code == 200
    assert TypeAdapter(list[object]).validate_json(response.content) == [
        {
            "id": str(provider.id),
            "provider_type": provider.provider_type,
            "name": provider.name,
            "is_active": provider.is_active,
        }
        for provider in providers
    ]
    lookup.assert_awaited_once_with(str(tenant_id) if scoped else None)


async def test_provider_list_does_not_add_response_coercion_or_null_rejection(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider_id = uuid4()
    provider = SimpleNamespace(
        id=provider_id, provider_type="custom", name=None, is_active="false", config={"secret": "private"}
    )
    monkeypatch.setattr(auth_provider_registry, "list_providers", AsyncMock(return_value=[provider]))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/auth/providers")

    assert response.status_code == 200
    assert TypeAdapter(list[object]).validate_json(response.content) == [
        {"id": str(provider_id), "provider_type": "custom", "name": None, "is_active": "false"}
    ]


@pytest.mark.parametrize(
    ("configured", "verified", "has_user", "active"),
    [
        (False, False, True, True),
        (True, None, False, True),
        (True, True, True, True),
        (True, False, False, True),
        (True, False, True, True),
        (True, False, True, False),
    ],
)
async def test_resend_verification_keeps_same_acknowledgement_for_every_account_state(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, configured: bool, verified: bool | None, has_user: bool, active: bool
) -> None:
    identity = (
        IdentityRecord(id=uuid4(), email_verified=bool(verified), is_active=active) if verified is not None else None
    )
    user = UserRecord(id=uuid4()) if has_user else None
    lookup = AsyncMock(return_value=identity)
    representative = AsyncMock(return_value=user)
    queue = AsyncMock()
    monkeypatch.setattr(
        system_email_service, "resolve_email_config_async", AsyncMock(return_value=object() if configured else None)
    )
    monkeypatch.setattr(auth.identity_dao, "get_by_email", lookup)
    monkeypatch.setattr(auth.user_dao, "get_representative_user_for_identity", representative)
    monkeypatch.setattr(auth, "_send_verification_email_task", queue)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/auth/resend-verification", json={"email": "member@example.com", "unknown": True})

    assert response.status_code == 200
    assert json_object_from_response(response) == {
        "ok": True,
        "message": "If an account with that email exists, a verification email has been sent.",
    }
    assert lookup.await_count == int(configured)
    assert representative.await_count == int(configured and verified is False)
    assert queue.await_count == int(configured and verified is False and has_user)


@pytest.mark.parametrize(
    ("method", "path", "fields", "location"),
    [
        ("GET", "/auth/providers", {"tenant_id": ""}, ["query", "tenant_id"]),
        ("GET", "/auth/providers", {"tenant_id": "not-a-uuid"}, ["query", "tenant_id"]),
        ("POST", "/auth/resend-verification", {}, ["body", "email"]),
        ("POST", "/auth/resend-verification", {"email": None}, ["body", "email"]),
        ("POST", "/auth/resend-verification", {"email": "not-an-email"}, ["body", "email"]),
    ],
)
async def test_remaining_auth_wrappers_preserve_request_validation_errors(
    app: FastAPI, method: str, path: str, fields: dict[str, str | None], location: list[str]
) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request(
            method, path, params=fields if method == "GET" else None, json=fields if method == "POST" else None
        )

    assert response.status_code == 422
    detail = json_object_from_response(response)["detail"]
    assert isinstance(detail, list)
    assert json_object_from(detail[0])["loc"] == location


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/auth/check-duplicate"), ("GET", "/auth/providers"), ("POST", "/auth/resend-verification")],
)
async def test_remaining_auth_wrappers_keep_backend_errors_as_server_errors(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, method: str, path: str
) -> None:
    monkeypatch.setattr(system_email_service, "resolve_email_config_async", AsyncMock(return_value=object()))
    monkeypatch.setattr(auth.identity_dao, "get_by_email", AsyncMock(side_effect=RuntimeError("private backend error")))
    monkeypatch.setattr(
        auth_provider_registry, "list_providers", AsyncMock(side_effect=RuntimeError("private backend error"))
    )

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        response = await client.request(
            method, path, params={"email": "member@example.com"}, json={"email": "member@example.com"}
        )

    assert response.status_code == 500
    assert response.text == "Internal Server Error"


async def test_resend_verification_keeps_acknowledgement_when_email_preparation_fails(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = IdentityRecord(id=uuid4(), email="member@example.com")
    user = UserRecord(id=uuid4(), identity_id=identity.id, identity=identity)
    monkeypatch.setattr(system_email_service, "resolve_email_config_async", AsyncMock(return_value=object()))
    monkeypatch.setattr(auth.identity_dao, "get_by_email", AsyncMock(return_value=identity))
    monkeypatch.setattr(auth.user_dao, "get_representative_user_for_identity", AsyncMock(return_value=user))
    monkeypatch.setattr("app.services.frontend_origin.resolve_frontend_base_url", AsyncMock(return_value="https://app"))
    monkeypatch.setattr(auth.identity_dao, "get", AsyncMock(side_effect=RuntimeError("private backend error")))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/auth/resend-verification", json={"email": "member@example.com"})

    assert response.status_code == 200
    assert json_object_from_response(response) == {
        "ok": True,
        "message": "If an account with that email exists, a verification email has been sent.",
    }


@pytest.mark.parametrize("fields", [{}, {"user": None, "identity": None, "suggested_org": None}])
def test_token_union_preserves_coercion_defaults_and_ignored_fields(fields: dict[str, object]) -> None:
    adapter = TypeAdapter(TokenResponse | MultiTenantResponse)

    result = adapter.validate_python({"access_token": "token", "needs_org_confirm": "1", "unknown": True, **fields})

    assert isinstance(result, TokenResponse)
    assert result.needs_org_confirm is True
    assert result.model_dump() == {
        "access_token": "token",
        "token_type": "bearer",
        "user": None,
        "identity": None,
        "tenant_name": None,
        "must_change_password": False,
        "needs_org_confirm": True,
        "suggested_org": None,
    }


def test_multitenant_union_preserves_nullable_choices_and_defaults() -> None:
    adapter = TypeAdapter(TokenResponse | MultiTenantResponse)

    result = adapter.validate_python(
        {
            "requires_tenant_selection": "true",
            "login_identifier": "",
            "unknown": True,
            "tenants": [{"tenant_id": None, "tenant_name": "Pending", "tenant_slug": "", "unknown": True}],
        }
    )

    assert isinstance(result, MultiTenantResponse)
    assert result.model_dump() == {
        "requires_tenant_selection": True,
        "login_identifier": "",
        "pending_token": None,
        "tenants": [{"tenant_id": None, "tenant_name": "Pending", "tenant_slug": "", "logo_url": None}],
    }


@pytest.mark.parametrize(
    ("contract", "fields", "expected"),
    [
        (RegistrationConfigResponse, {"invitation_code_required": "false"}, {"invitation_code_required": False}),
        (ForgotPasswordResponse, {"ok": "true", "message": "message"}, {"ok": True, "message": "message"}),
        (ResetPasswordResponse, {"ok": "1"}, {"ok": True}),
        (ResendVerificationResponse, {"ok": "true", "message": "message"}, {"ok": True, "message": "message"}),
        (
            EmailConflict,
            {"type": "email", "scope": "global", "message": "conflict"},
            {"type": "email", "scope": "global", "message": "conflict"},
        ),
        (
            CheckDuplicateResponse,
            {"email_exists": "0", "conflicts": [], "has_conflict": "false"},
            {"email_exists": False, "conflicts": [], "has_conflict": False},
        ),
        (
            CheckDuplicateResponse,
            {
                "email_exists": "1",
                "conflicts": [{"type": "email", "scope": "global", "message": "conflict", "extra": None}],
                "has_conflict": True,
            },
            {
                "email_exists": True,
                "conflicts": [{"type": "email", "scope": "global", "message": "conflict"}],
                "has_conflict": True,
            },
        ),
        (
            AuthProviderResponse,
            {"id": "provider-id", "provider_type": "custom", "name": "", "is_active": "0"},
            {"id": "provider-id", "provider_type": "custom", "name": "", "is_active": False},
        ),
    ],
)
def test_dictionary_contracts_keep_coercion_and_ignored_extras(
    contract: type, fields: dict[str, object], expected: dict[str, object]
) -> None:
    result = TypeAdapter[object](contract).validate_python({**fields, "unknown": True})

    assert isinstance(result, dict)
    assert result == expected


@pytest.mark.parametrize("fields", [{}, {"ok": None}])
def test_reset_dictionary_requires_non_null_success_flag(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(ResetPasswordResponse).validate_python(fields)


@pytest.mark.parametrize(
    ("contract", "fields"),
    [
        (CheckDuplicateResponse, {}),
        (CheckDuplicateResponse, {"email_exists": False, "conflicts": None, "has_conflict": False}),
        (CheckDuplicateResponse, {"email_exists": None, "conflicts": [], "has_conflict": False}),
        (CheckDuplicateResponse, {"email_exists": False, "conflicts": [], "has_conflict": None}),
        (EmailConflict, {"type": "phone", "scope": "global", "message": "conflict"}),
        (EmailConflict, {"type": "email", "scope": "tenant", "message": "conflict"}),
        (EmailConflict, {"type": "email", "scope": "global", "message": None}),
        (AuthProviderResponse, {"id": "provider", "provider_type": "custom", "is_active": True}),
        (AuthProviderResponse, {"id": "provider", "provider_type": "custom", "name": None, "is_active": True}),
        (AuthProviderResponse, {"id": "provider", "provider_type": "custom", "name": "", "is_active": None}),
        (ResendVerificationResponse, {}),
        (ResendVerificationResponse, {"ok": True}),
        (ResendVerificationResponse, {"ok": None, "message": "message"}),
        (ResendVerificationResponse, {"ok": True, "message": None}),
    ],
)
def test_remaining_dictionary_contracts_require_complete_non_null_fields(
    contract: type, fields: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter[object](contract).validate_python(fields)
