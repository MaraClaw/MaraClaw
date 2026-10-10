from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api import admin, auth, tenants
from app.core.security import get_current_user
from app.records.user import UserRecord


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/auth/register"),
        ("POST", "/api/auth/register/sso"),
        ("GET", "/api/auth/email-hint?username=ada"),
    ],
)
async def test_retired_registration_routes_return_404(method: str, path: str) -> None:
    # Given the production auth router mounted at its public prefix.
    app = FastAPI()
    app.include_router(auth.router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # When a retired endpoint is requested.
        response = await client.request(method, path, json={})

    # Then no compatibility handler remains available.
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_registration_config_exposes_platform_admin_creation_only() -> None:
    # Given the production public tenant router.
    app = FastAPI()
    app.include_router(tenants.router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # When registration configuration is requested.
        response = await client.get("/api/tenants/registration-config")

    # Then no inert self-creation toggle is advertised.
    assert response.status_code == 200
    assert response.json() == {"tenant_creation": "platform_admin_only"}


@pytest.mark.asyncio
async def test_retired_self_creation_setting_cannot_be_persisted(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given an authenticated platform admin and the production settings router.
    app = FastAPI()
    app.include_router(admin.router, prefix="/api")

    async def current_user() -> UserRecord:
        return UserRecord(id=uuid4(), role="platform_admin")

    app.dependency_overrides[get_current_user] = current_user
    set_flag = AsyncMock()
    monkeypatch.setattr(admin.system_setting_dao, "set_flag", set_flag)
    monkeypatch.setattr(admin.system_setting_dao, "is_flag_enabled", AsyncMock(return_value=False))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # When the retired toggle is submitted.
        response = await client.put("/api/admin/platform-settings", json={"allow_self_create_company": True})

    app.dependency_overrides.clear()
    # Then the ignored unknown field cannot reach storage or the response contract.
    assert response.status_code == 200
    assert response.json() == {"invitation_code_enabled": False, "sso_custom_domain_redirect_enabled": False}
    set_flag.assert_not_awaited()
