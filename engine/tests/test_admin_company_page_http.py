"""Company list routes: authorization before cursor decoding, cursors, pickers and legacy shape."""

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

from app.api import admin
from app.core import security
from app.core.domain.users import UserRole
from app.dao.tenant_dao import TenantUsageStats, tenant_dao
from app.records.identity import IdentityRecord
from app.records.tenant import TenantRecord
from app.records.user import UserRecord

_BASE = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
_PAGE = "/api/admin/companies/page"


def _tenant(number: int, **kwargs: Any) -> TenantRecord:
    return TenantRecord(
        id=UUID(int=number),
        name=f"Co {number}",
        slug=f"co-{number}",
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
        security, "decode_access_token", lambda _: {"sub": str(user.id), "role": "member", "exp": 0, "gen": 1}
    )
    monkeypatch.setattr(security.user_dao, "get_with_identity", AsyncMock(return_value=user))
    monkeypatch.setattr(security.identity_dao, "get_auth_generation", AsyncMock(return_value=1))
    return user


@pytest.fixture
def pages(monkeypatch) -> dict[str, AsyncMock]:
    tenants = [_tenant(number) for number in (1, 2, 3)]
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


@pytest.mark.parametrize("path", [_PAGE, "/api/admin/companies/options", f"/api/admin/companies/{uuid4()}/stats"])
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
    response = await client.get(path, params={"cursor": "!"} if path == _PAGE else None, headers=headers)

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
async def test_malformed_or_forged_cursor_is_400(client, principal, pages, cursor):
    response = await client.get(_PAGE, params={"cursor": cursor}, headers={"Authorization": "Bearer t"})
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


@pytest.mark.parametrize(("limit", "expected"), [(0, 1), (-5, 1), (9999, 200)])
async def test_page_limit_is_clamped(client, principal, pages, limit, expected):
    _ = await client.get(_PAGE, params={"limit": limit}, headers={"Authorization": "Bearer t"})
    assert pages["list_page"].await_args.kwargs["limit"] == expected


async def test_search_returns_ranked_list_without_cursor_and_rejects_a_cursor(client, principal, pages):
    headers = {"Authorization": "Bearer t"}
    found = await client.get(_PAGE, params={"q": " co ", "limit": 1}, headers=headers)
    assert found.json()["next_cursor"] is None
    assert len(found.json()["items"]) == 1
    pages["search_by_name"].assert_awaited_once_with("co")
    pages["list_page"].assert_not_awaited()

    with_cursor = await client.get(_PAGE, params={"q": "co", "cursor": "anything"}, headers=headers)
    too_long = await client.get(_PAGE, params={"q": "x" * 201}, headers=headers)
    assert (with_cursor.status_code, too_long.status_code) == (400, 400)


async def test_options_are_stats_free_and_stats_route_reports_404(client, principal, pages):
    headers = {"Authorization": "Bearer t"}
    options = (await client.get("/api/admin/companies/options", headers=headers)).json()
    assert [row["id"] for row in options] == [str(UUID(int=n)) for n in (1, 2, 3)]
    assert "user_count" not in options[0]
    assert options[0]["can_disable"] is True
    pages["usage_stats"].assert_not_awaited()

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
