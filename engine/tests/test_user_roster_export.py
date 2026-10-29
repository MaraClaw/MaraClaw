import csv
from io import StringIO
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI

from app.api import users as users_api
from app.core import security
from app.core.domain.users import UserRole
from app.core.security import get_current_user
from app.records.identity import IdentityRecord
from app.records.tenant import TenantRecord
from app.records.user import UserRecord
from app.services.user_roster_export import _text_cell

TENANT = UUID(int=100)
FOREIGN = UUID(int=200)


@pytest.mark.parametrize(
    "text",
    ["=1+1", "+SUM(A1)", "-1", "@cmd", "  =1", " \t+1", "\u2003@cmd", "\tordinary", "\rordinary", "\nordinary"],
)
def test_prefixes_hostile_text_when_cell_has_formula_or_leading_control(text: str) -> None:
    assert _text_cell(text) == "'" + text


@pytest.mark.parametrize(
    "text", ["", "Alice", "  Alice  ", "'already text", "é,中文", 'quoted "text"', "middle\nline", "a=b", " \tordinary"]
)
def test_preserves_text_when_cell_does_not_match_formula_rule(text: str) -> None:
    assert _text_cell(text) == text


async def _request(actor: UserRecord | None, params: dict[str, str]) -> httpx.Response:
    app = FastAPI()
    app.include_router(users_api.export_router, prefix="/api")
    app.include_router(users_api.router, prefix="/api")
    if actor is not None:
        app.dependency_overrides[get_current_user] = lambda: actor
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.get("/api/users/export", params=params)


@pytest.fixture
def exporter(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    export = AsyncMock(return_value="id,email,display_name,role,is_active,agents_count,created_at,source\r\n")
    monkeypatch.setattr(users_api, "export_user_roster", export)
    monkeypatch.setattr(
        users_api.tenant_dao, "get", AsyncMock(return_value=TenantRecord(id=TENANT, name="Own", slug="own"))
    )
    return export


@pytest.mark.parametrize("supplied", [{}, {"tenant_id": str(FOREIGN)}, {"tenant_id": "invalid"}])
async def test_uses_own_company_when_org_admin_exports_literal_search(exporter: AsyncMock, supplied: dict[str, str]):
    actor = UserRecord(id=UUID(int=1), tenant_id=TENANT, role=UserRole.ORG_ADMIN)
    response = await _request(actor, {**supplied, "search": "  %_\\  "})
    assert response.status_code == 200
    envelope = users_api.UserRosterExportOut.model_validate_json(response.content)
    assert next(csv.reader(StringIO(envelope.csv))) == [
        "id",
        "email",
        "display_name",
        "role",
        "is_active",
        "agents_count",
        "created_at",
        "source",
    ]
    exporter.assert_awaited_once_with(TENANT, "%_\\", actor)


@pytest.mark.parametrize(
    ("role", "tenant", "params", "status"),
    [
        ("member", TENANT, {}, 403),
        ("agent_admin", TENANT, {}, 403),
        ("org_admin", None, {"tenant_id": str(TENANT)}, 403),
        ("platform_admin", None, {}, 400),
        ("platform_admin", None, {"tenant_id": ""}, 400),
        ("platform_admin", None, {"tenant_id": "invalid"}, 400),
    ],
)
async def test_denies_export_when_role_or_scope_is_invalid(
    exporter: AsyncMock, role: str, tenant: UUID | None, params: dict[str, str], status: int
):
    response = await _request(UserRecord(id=UUID(int=1), tenant_id=tenant, role=UserRole(role)), params)
    assert response.status_code == status
    exporter.assert_not_awaited()


async def test_denies_export_when_anonymous(exporter: AsyncMock):
    response = await _request(None, {})
    assert response.status_code == 401
    exporter.assert_not_awaited()


async def test_exports_selected_company_when_platform_admin_is_authorized(exporter: AsyncMock):
    actor = UserRecord(id=UUID(int=1), tenant_id=None, role=UserRole.PLATFORM_ADMIN)
    response = await _request(actor, {"tenant_id": str(FOREIGN), "search": "  "})
    assert response.status_code == 200
    exporter.assert_awaited_once_with(FOREIGN, "", actor)


async def test_rejects_company_when_uuid_has_no_company(exporter: AsyncMock, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(users_api.tenant_dao, "get", AsyncMock(return_value=None))
    response = await _request(UserRecord(id=UUID(int=1), role=UserRole.PLATFORM_ADMIN), {"tenant_id": str(FOREIGN)})
    assert response.status_code == 404
    exporter.assert_not_awaited()


async def test_denies_export_when_password_change_is_required(exporter: AsyncMock, monkeypatch: pytest.MonkeyPatch):
    actor = UserRecord(
        id=UUID(int=1),
        tenant_id=TENANT,
        role=UserRole.ORG_ADMIN,
        identity_id=UUID(int=1),
        identity=IdentityRecord(id=UUID(int=1), must_change_password=True),
    )
    monkeypatch.setattr(security, "decode_access_token", lambda _token: {"sub": str(actor.id), "gen": 7})
    monkeypatch.setattr(security.user_dao, "get_with_identity", AsyncMock(return_value=actor))
    monkeypatch.setattr(security.identity_dao, "get_auth_generation", AsyncMock(return_value=7))
    app = FastAPI()
    app.include_router(users_api.export_router, prefix="/api")
    app.include_router(users_api.router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/users/export", headers={"Authorization": "Bearer test-token"})
    assert response.status_code == 403
    exporter.assert_not_awaited()
