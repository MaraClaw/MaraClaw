from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import admin, users
from app.core import security
from app.core.json_types import object_mapping_from
from app.dao.admin_audit_dao import admin_audit_log_dao
from app.records.audit import AdminAuditLogRecord
from app.records.identity import IdentityRecord
from app.records.user import UserRecord

_PLATFORM = "/api/admin/audit-logs"
_ORG = "/api/users/admin-audit-logs"


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    app.include_router(admin.router, prefix="/api")
    app.include_router(users.router, prefix="/api")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://audit.test") as client:
        yield client


@pytest.fixture
def principal(monkeypatch) -> UserRecord:
    identity = IdentityRecord(id=uuid4())
    user = UserRecord(id=uuid4(), identity_id=identity.id, tenant_id=uuid4(), identity=identity)
    monkeypatch.setattr(
        security, "decode_access_token", lambda _: {"sub": str(user.id), "role": "member", "exp": 0, "gen": 1}
    )
    monkeypatch.setattr(security.user_dao, "get_with_identity", AsyncMock(return_value=user))
    monkeypatch.setattr(security.identity_dao, "get_auth_generation", AsyncMock(return_value=1))
    return user


@pytest.mark.parametrize(("path", "role"), [(_PLATFORM, "platform_admin"), (_ORG, "org_admin")])
@pytest.mark.parametrize(
    ("state", "status"),
    [("missing", 401), ("retired", 401), ("inactive", 401), ("force_change", 403), ("wrong_role", 403), ("valid", 400)],
)
@pytest.mark.parametrize("cursor", ["!", "x" * 1025])
async def test_app_auth_and_force_change_precede_cursor_decoding(
    client, principal, monkeypatch, path, role, state, status, cursor
):
    # Given the real auth dependency chain and one unusable cursor.
    principal.role = role
    assert principal.identity is not None
    match state:
        case "retired":
            monkeypatch.setattr(security.identity_dao, "get_auth_generation", AsyncMock(return_value=2))
        case "inactive":
            principal.is_active = False
        case "force_change":
            principal.identity.must_change_password = True
        case "wrong_role":
            principal.role = "member"
    query = AsyncMock()
    monkeypatch.setattr(admin_audit_log_dao, "list_page", query)
    headers = {} if state == "missing" else {"Authorization": "Bearer fixture-token"}
    # When the request passes through the app, not a handler-direct shortcut.
    response = await client.get(path + "/page", params={"cursor": cursor}, headers=headers)
    # Then authorization/retirement wins and SQL is never reached.
    assert response.status_code == status, response.text
    if state == "force_change":
        assert response.json()["detail"]["must_change_password"] is True
    query.assert_not_awaited()


@pytest.mark.parametrize(("path", "role"), [(_PLATFORM, "platform_admin"), (_ORG, "org_admin")])
async def test_array_endpoints_are_unchanged_and_page_items_match(client, principal, monkeypatch, path, role):
    # Given one persisted-shaped event and an authorized caller.
    principal.role = role
    row = AdminAuditLogRecord(
        id=uuid4(),
        actor_id=principal.id,
        actor_role=role,
        actor_email=None,
        action="tenant_create",
        target_type="tenant",
        target_id=principal.tenant_id,
        tenant_id=principal.tenant_id,
        changes={"name": {"before": None, "after": "Acme"}},
        details={},
        created_at=datetime(2026, 10, 24, 12, 0, 0, 123456, tzinfo=UTC),
    )
    recent = AsyncMock(return_value=[row])
    page_query = AsyncMock(return_value=[row])
    monkeypatch.setattr(admin_audit_log_dao, "list_recent", recent)
    monkeypatch.setattr(admin_audit_log_dao, "list_page", page_query)
    headers = {"Authorization": "Bearer fixture-token"}
    params = {"action": "tenant_create", "limit": 0, "tenant_id": str(uuid4()), "actor_id": str(principal.id)}
    # When existing and additive clients request the same event.
    legacy = await client.get(path, params={**params, "start_at": "invalid", "end_at": "0"}, headers=headers)
    page = await client.get(path + "/page", params=params, headers=headers)
    # Then the old API is still an array with the same item fields and precision.
    assert legacy.status_code == page.status_code == 200
    assert isinstance(legacy.json(), list)
    assert page.json() == {"items": legacy.json(), "next_cursor": None}
    assert recent.await_args is not None
    assert page_query.await_args is not None
    expected_tenant = principal.tenant_id if role == "org_admin" else UUID(params["tenant_id"])
    assert recent.await_args.kwargs["tenant_id"] == page_query.await_args.kwargs["tenant_id"] == expected_tenant
    assert recent.await_args.kwargs["limit"] == page_query.await_args.kwargs["limit"] == 1
    assert page_query.await_args.kwargs["actor_id"] == (None if role == "org_admin" else principal.id)


async def test_identity_platform_admin_uses_effective_platform_cursor_scope(client, principal, monkeypatch):
    # Given a membership whose loaded identity grants platform-admin access.
    assert principal.identity is not None
    principal.identity.is_platform_admin = True
    monkeypatch.setattr(admin_audit_log_dao, "list_page", AsyncMock(return_value=[]))
    # When that identity uses the platform page endpoint.
    response = await client.get(_PLATFORM + "/page", headers={"Authorization": "Bearer fixture-token"})
    # Then the existing identity-based role exception remains supported.
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


@pytest.mark.parametrize(("path", "role"), [(_PLATFORM, "platform_admin"), (_ORG, "org_admin")])
@pytest.mark.parametrize("field", ["start_at", "end_at"])
@pytest.mark.parametrize(
    "value",
    [
        "",
        "0",
        "1792843200",
        "1792843200.5",
        "2026-10-24",
        "2026-10-24T12:00:00",
        "not-a-date",
        "2026-02-30T12:00:00Z",
        "2026-10-24 12:00:00Z",
        "20261024T120000Z",
        "2026-10-24T12:00:00+0000",
        "2026-10-24T12:00:00+00:60",
        "2026-10-24T12:00:00+24:00",
        "2026-10-24T12:00:00Z ",
        "0001-01-01T00:00:00+01:00",
        "9999-12-31T23:59:59-01:00",
    ],
)
async def test_page_rejects_non_rfc3339_text_bounds(client, principal, monkeypatch, path, role, field, value):
    # Given an authorized reader and a malformed or numeric textual query bound.
    principal.role = role
    query = AsyncMock(return_value=[])
    monkeypatch.setattr(admin_audit_log_dao, "list_page", query)
    # When either paged route receives the bound.
    response = await client.get(
        path + "/page", params={field: value}, headers={"Authorization": "Bearer fixture-token"}
    )
    # Then no datetime coercion or SQL is permitted.
    assert response.status_code == 422, response.text
    query.assert_not_awaited()


@pytest.mark.parametrize(("path", "role"), [(_PLATFORM, "platform_admin"), (_ORG, "org_admin")])
@pytest.mark.parametrize("end_at", ["2026-10-24T14:00:00+02:00", "2026-10-24T11:59:59Z"])
async def test_page_rejects_equal_or_reversed_instants(client, principal, monkeypatch, path, role, end_at):
    # Given equal or reversed instants, regardless of textual offset.
    principal.role = role
    query = AsyncMock(return_value=[])
    monkeypatch.setattr(admin_audit_log_dao, "list_page", query)
    # When the interval is requested.
    response = await client.get(
        path + "/page",
        params={"start_at": "2026-10-24T12:00:00Z", "end_at": end_at},
        headers={"Authorization": "Bearer fixture-token"},
    )
    # Then invalid intervals never reach SQL.
    assert response.status_code == 422, response.text
    query.assert_not_awaited()


@pytest.mark.parametrize(("path", "role"), [(_PLATFORM, "platform_admin"), (_ORG, "org_admin")])
@pytest.mark.parametrize("field", ["start_at", "end_at"])
@pytest.mark.parametrize(
    "value",
    ["2026-10-24T14:00:00.123456+02:00", "2026-10-24t07:00:00.123456-05:00", "2026-10-24t12:00:00.123456z"],
)
async def test_page_normalizes_each_optional_bound_to_utc(client, principal, monkeypatch, path, role, field, value):
    # Given a valid offset-bearing bound with the other bound absent.
    principal.role = role
    query = AsyncMock(return_value=[])
    monkeypatch.setattr(admin_audit_log_dao, "list_page", query)
    # When either administrator requests the interval.
    response = await client.get(
        path + "/page",
        params={field: value, "tenant_id": str(uuid4())},
        headers={"Authorization": "Bearer fixture-token"},
    )
    # Then SQL receives an aware UTC instant and an explicit absent other bound.
    assert response.status_code == 200, response.text
    assert query.await_args is not None
    bounds = object_mapping_from(query.await_args.kwargs)
    bound = bounds[field]
    assert isinstance(bound, datetime)
    assert bound == datetime(2026, 10, 24, 12, 0, 0, 123456, tzinfo=UTC)
    assert bound.tzinfo is UTC
    assert bounds["end_at" if field == "start_at" else "start_at"] is None
    if role == "org_admin":
        assert bounds["tenant_id"] == principal.tenant_id
