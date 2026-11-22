"""Company list routes: authorization before cursor decoding, cursors, picker pages and legacy shape."""

from base64 import urlsafe_b64encode
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.membership_generation_fixtures import pin_membership_generation

from app.api import admin
from app.core import security
from app.core.domain.users import UserRole
from app.dao.tenant_dao import TenantUsageStats, tenant_dao
from app.records.identity import IdentityRecord
from app.records.organization_plan import OrganizationPlanId
from app.records.tenant import TenantRecord
from app.records.user import UserRecord

_BASE = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
_PAGE = "/api/admin/companies/page"
_OPTIONS = "/api/admin/companies/options"
_PHONE = "+84912345678"


def _tenant(number: int, tier: OrganizationPlanId = OrganizationPlanId.PRO, **kwargs: Any) -> TenantRecord:
    return TenantRecord(
        id=UUID(int=number),
        name=f"Co {number}",
        slug=f"co-{number}",
        tier=tier,
        created_at=_BASE - timedelta(days=number),
        **kwargs,
    )


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    app.include_router(admin.router, prefix="/api")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://companies.test") as client:
        yield client


@pytest.fixture
def principal(monkeypatch) -> UserRecord:
    identity = IdentityRecord(id=uuid4())
    user = UserRecord(
        id=uuid4(), identity_id=identity.id, tenant_id=uuid4(), identity=identity, role=UserRole.PLATFORM_ADMIN
    )
    monkeypatch.setattr(
        security,
        "decode_access_token",
        lambda _: {"sub": str(user.id), "role": "member", "exp": 0, "gen": 1, "mgen": 0},
    )
    monkeypatch.setattr(security.user_dao, "get_with_identity", AsyncMock(return_value=user))
    monkeypatch.setattr(security.identity_dao, "get_auth_generation", AsyncMock(return_value=1))
    pin_membership_generation(monkeypatch)
    return user


@pytest.fixture
def pages(monkeypatch) -> dict[str, AsyncMock]:
    tenants = [
        _tenant(1, OrganizationPlanId.MAX, phone=_PHONE),
        _tenant(2, OrganizationPlanId.PRO),
        _tenant(3, OrganizationPlanId.ULTRA),
    ]
    mocks = {
        "list_page": AsyncMock(return_value=tenants),
        "usage_stats": AsyncMock(return_value={tenants[0].id: TenantUsageStats(user_count=2, active_user_count=1)}),
        "list_ordered_by_created_at": AsyncMock(return_value=tenants),
        "search_by_name": AsyncMock(return_value=tenants[:1]),
        "get": AsyncMock(return_value=tenants[0]),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(tenant_dao, name, mock)
    return mocks


@pytest.mark.parametrize("path", [_PAGE, _OPTIONS, f"/api/admin/companies/{uuid4()}/stats"])
@pytest.mark.parametrize("state", ["missing", "wrong_role", "force_change"])
async def test_every_company_read_requires_platform_admin(client, principal, pages, path, state):
    # Given a caller who is not a usable platform admin, and a cursor that would fail to decode.
    headers = {"Authorization": "Bearer t"}
    match state:
        case "missing":
            headers = {}
        case "wrong_role":
            principal.role = UserRole.ORG_ADMIN
        case "force_change":
            assert principal.identity is not None
            principal.identity.must_change_password = True

    # When the route is called.
    response = await client.get(path, params={"cursor": "!"} if path in (_PAGE, _OPTIONS) else None, headers=headers)

    # Then authorization answers first and nothing is read.
    assert response.status_code == (401 if state == "missing" else 403)
    for mock in pages.values():
        mock.assert_not_awaited()


@pytest.mark.parametrize(
    "cursor",
    [
        "!",
        "x" * 1025,
        urlsafe_b64encode(b"not json").decode(),
        urlsafe_b64encode(
            b'{"version":1,"created_at":"2026-10-01T00:00:00Z","id":"%b","signature":"%b"}' % (b"x", b"0" * 64)
        ).decode(),
        urlsafe_b64encode(
            b'{"version":1,"created_at":"2026-10-01T00:00:00Z","id":"%b","signature":"%b"}'
            % (str(uuid4()).encode(), b"0" * 64)
        ).decode(),
    ],
)
@pytest.mark.parametrize("path", [_PAGE, _OPTIONS])
async def test_malformed_or_forged_cursor_is_400(client, principal, pages, path, cursor):
    response = await client.get(path, params={"cursor": cursor}, headers={"Authorization": "Bearer t"})
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid cursor"
    pages["list_page"].assert_not_awaited()


async def test_cursor_round_trips_and_seeks_after_last_item(client, principal, pages):
    headers = {"Authorization": "Bearer t"}

    # When the first page has a lookahead row.
    first = (await client.get(_PAGE, params={"limit": 2}, headers=headers)).json()

    # Then it trims to the limit, signs the boundary and keeps zero stats for unknown ids.
    assert [item["id"] for item in first["items"]] == [str(UUID(int=1)), str(UUID(int=2))]
    assert first["items"][0]["user_count"] == 2
    assert first["items"][0]["inactive_user_count"] == 1
    assert first["items"][1]["user_count"] == 0
    assert pages["list_page"].await_args.kwargs == {"limit": 2, "before": None}
    assert pages["usage_stats"].await_args.args == ([UUID(int=1), UUID(int=2)],)

    # When the cursor is replayed, the seek starts after the second tenant.
    pages["list_page"].return_value = [_tenant(3)]
    last = (await client.get(_PAGE, params={"limit": 2, "cursor": first["next_cursor"]}, headers=headers)).json()
    assert pages["list_page"].await_args.kwargs == {"limit": 2, "before": (_tenant(2).created_at, UUID(int=2))}
    assert last["next_cursor"] is None


@pytest.mark.parametrize("path", [_PAGE, _OPTIONS])
@pytest.mark.parametrize(("limit", "expected"), [(None, 50), (0, 1), (-5, 1), (9999, 200)])
async def test_page_limit_is_clamped(client, principal, pages, path, limit, expected):
    params = {} if limit is None else {"limit": limit}
    _ = await client.get(path, params=params, headers={"Authorization": "Bearer t"})
    assert pages["list_page"].await_args.kwargs["limit"] == expected


@pytest.mark.parametrize("path", [_PAGE, _OPTIONS])
async def test_search_returns_ranked_list_without_cursor_and_rejects_a_cursor(client, principal, pages, path):
    headers = {"Authorization": "Bearer t"}
    found = await client.get(path, params={"q": " co ", "limit": 1}, headers=headers)
    assert found.json()["next_cursor"] is None
    assert len(found.json()["items"]) == 1
    pages["search_by_name"].assert_awaited_once_with("co")
    pages["list_page"].assert_not_awaited()

    with_cursor = await client.get(path, params={"q": "co", "cursor": "anything"}, headers=headers)
    too_long = await client.get(path, params={"q": "x" * 201}, headers=headers)
    assert (with_cursor.status_code, too_long.status_code) == (400, 400)
    assert with_cursor.json()["detail"] == "Cursor cannot be combined with search"
    pages["list_ordered_by_created_at"].assert_not_awaited()


async def test_company_rows_carry_the_tier_of_their_tenant_row(client, principal, pages):
    headers = {"Authorization": "Bearer t"}

    # When a page of three is read, one company's stats and the unpaged list.
    page = (await client.get(_PAGE, params={"limit": 3}, headers=headers)).json()
    single = (await client.get(f"/api/admin/companies/{UUID(int=1)}/stats", headers=headers)).json()
    unpaged = (await client.get("/api/admin/companies", headers=headers)).json()

    # Then every row carries the tier stored on its tenant.
    assert [row["tier"] for row in page["items"]] == ["max", "pro", "ultra"]
    assert single["tier"] == "max"
    assert [row["tier"] for row in unpaged] == ["max", "pro", "ultra"]


async def test_company_rows_carry_the_contact_phone_of_their_tenant_row(client, principal, pages):
    headers = {"Authorization": "Bearer t"}

    # When a page, one company's stats and the unpaged list are read (the other tenants have no phone).
    page = (await client.get(_PAGE, params={"limit": 3}, headers=headers)).json()
    single = (await client.get(f"/api/admin/companies/{UUID(int=1)}/stats", headers=headers)).json()
    unpaged = (await client.get("/api/admin/companies", headers=headers)).json()

    # Then every row carries the phone stored on its tenant, or null.
    assert [row["phone"] for row in page["items"]] == [_PHONE, None, None]
    assert single["phone"] == _PHONE
    assert [row["phone"] for row in unpaged] == [_PHONE, None, None]


async def test_options_are_stats_free_keyset_pages_that_never_read_every_company(client, principal, pages):
    headers = {"Authorization": "Bearer t"}

    # When the first picker page has a lookahead row.
    first = (await client.get(_OPTIONS, params={"limit": 2}, headers=headers)).json()

    # Then it trims to the limit, carries no stats and signs the boundary.
    assert set(first) == {"items", "next_cursor"}
    assert [row["id"] for row in first["items"]] == [str(UUID(int=n)) for n in (1, 2)]
    assert "user_count" not in first["items"][0]
    assert "tier" not in first["items"][0]
    assert "phone" not in first["items"][0]
    assert first["items"][0]["can_disable"] is True
    assert pages["list_page"].await_args.kwargs == {"limit": 2, "before": None}

    # When the cursor is replayed, the seek starts after the second tenant and the last page has no cursor.
    pages["list_page"].return_value = [_tenant(3)]
    last = (await client.get(_OPTIONS, params={"limit": 2, "cursor": first["next_cursor"]}, headers=headers)).json()
    assert pages["list_page"].await_args.kwargs == {"limit": 2, "before": (_tenant(2).created_at, UUID(int=2))}
    assert [row["id"] for row in last["items"]] == [str(UUID(int=3))]
    assert last["next_cursor"] is None

    # And neither stats nor the full company list were read.
    pages["usage_stats"].assert_not_awaited()
    pages["list_ordered_by_created_at"].assert_not_awaited()


async def test_stats_route_reports_404(client, principal, pages):
    headers = {"Authorization": "Bearer t"}
    found = await client.get(f"/api/admin/companies/{UUID(int=1)}/stats", headers=headers)
    assert found.json()["user_count"] == 2
    pages["get"].return_value = None
    assert (await client.get(f"/api/admin/companies/{uuid4()}/stats", headers=headers)).status_code == 404


async def test_legacy_list_stays_a_bare_array_with_system_orgs_not_disableable(client, principal, pages):
    pages["list_ordered_by_created_at"].return_value = [_tenant(1, is_system=True), _tenant(2)]
    body = (await client.get("/api/admin/companies", headers={"Authorization": "Bearer t"})).json()
    assert isinstance(body, list)
    assert [row["can_disable"] for row in body] == [False, True]
    assert body[0]["user_count"] == 2
    assert body[1]["org_admin_email"] is None


async def test_usage_stats_is_one_statement_whatever_the_batch_size(monkeypatch):
    # Given a fake session that counts statements.
    statements: list[tuple[str, Any]] = []

    class _Db:
        async def fetchall(self, query: str, params: Any = None) -> list[dict[str, Any]]:
            statements.append((query, params))
            return []

    @asynccontextmanager
    async def _session() -> AsyncGenerator[_Db]:
        yield _Db()

    monkeypatch.setattr(tenant_dao, "session", _session)

    # When 3 and 300 tenants are summarised, and none.
    small = await tenant_dao.usage_stats([uuid4() for _ in range(3)])
    large = await tenant_dao.usage_stats([uuid4() for _ in range(300)])
    none = await tenant_dao.usage_stats([])

    # Then each batch costs one statement and an empty batch costs none.
    assert (small, large, none) == ({}, {}, {})
    assert len(statements) == 2
