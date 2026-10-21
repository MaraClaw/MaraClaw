import json
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Mapping
from datetime import UTC, datetime
from hashlib import sha256
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app.api import users as users_api
from app.core import security
from app.core.security import get_current_user
from app.dao.user_dao import UserPagePosition
from app.records.identity import IdentityRecord
from app.records.user import UserRecord

TENANT = UUID(int=100)
FOREIGN = UUID(int=200)
NOW = datetime(2026, 10, 20, tzinfo=UTC)


def _actor(role: str = "org_admin", tenant_id: UUID | None = TENANT) -> UserRecord:
    return UserRecord(id=UUID(int=1), role=role, tenant_id=tenant_id)


def _cursor(**changes: object) -> str:
    payload = {
        "version": 1,
        "tenant_id": str(TENANT),
        "search_digest": sha256(b"").hexdigest(),
        "role_rank": 0,
        "created_at": NOW.isoformat(),
        "user_id": str(UUID(int=10)),
    }
    payload.update(changes)
    return urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


async def _request(actor: UserRecord, params: Mapping[str, str | int]) -> httpx.Response:
    app = FastAPI()
    app.include_router(users_api.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: actor
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.get("/api/users/page", params=params)


@pytest.fixture
def page_query(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    query = AsyncMock(return_value=[])
    monkeypatch.setattr(users_api.user_dao, "list_for_tenant_page", query)
    monkeypatch.setattr(users_api.agent_dao, "count_active_for_creators", AsyncMock(return_value={}))
    return query


@pytest.mark.parametrize("supplied", [{}, {"tenant_id": str(FOREIGN)}, {"tenant_id": "not-a-uuid"}])
async def test_own_tenant_when_org_admin_supplies_foreign_filter(page_query: AsyncMock, supplied: dict[str, str]):
    response = await _request(_actor(), {**supplied, "search": "  Alice  "})
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}
    page_query.assert_awaited_once_with(TENANT, search="Alice", after=None, limit=50)


@pytest.mark.parametrize("role", ["member", "agent_admin", "guest"])
async def test_rejects_non_admin_when_requesting_page(page_query: AsyncMock, role: str):
    response = await _request(_actor(role), {})
    assert response.status_code == 403
    page_query.assert_not_awaited()


async def test_rejects_org_admin_when_company_is_missing(page_query: AsyncMock):
    response = await _request(_actor(tenant_id=None), {"tenant_id": str(FOREIGN)})
    assert response.status_code == 403
    page_query.assert_not_awaited()


@pytest.mark.parametrize("params", [{}, {"tenant_id": ""}, {"tenant_id": "invalid"}])
async def test_rejects_platform_admin_when_company_selection_is_invalid(page_query: AsyncMock, params: dict[str, str]):
    response = await _request(_actor("platform_admin", None), params)
    assert response.status_code == 400
    page_query.assert_not_awaited()


async def test_selected_tenant_when_platform_admin_requests_maximum_page(page_query: AsyncMock):
    response = await _request(_actor("platform_admin"), {"tenant_id": str(FOREIGN), "limit": 100})
    assert response.status_code == 200
    page_query.assert_awaited_once_with(FOREIGN, search="", after=None, limit=100)


@pytest.mark.parametrize("limit", [0, -1, 101, "invalid", "1.5"])
async def test_rejects_page_size_when_outside_bounds(page_query: AsyncMock, limit: str | int):
    response = await _request(_actor(), {"limit": limit})
    assert response.status_code == 422
    page_query.assert_not_awaited()


@pytest.mark.parametrize(
    "cursor",
    [
        "",
        "a",
        "not%base64",
        "e30",
        "W10",
        "_w",
        _cursor(version=2),
        _cursor(role_rank=2),
        _cursor(role_rank=True),
        _cursor(created_at="2026-10-20T00:00:00"),
        _cursor(user_id="invalid"),
        _cursor(search_digest="invalid"),
        _cursor(unexpected=True),
    ],
)
async def test_rejects_cursor_when_encoding_or_payload_is_invalid(page_query: AsyncMock, cursor: str):
    response = await _request(_actor(), {"cursor": cursor})
    assert response.status_code == 400
    page_query.assert_not_awaited()


async def test_rejects_cursor_when_over_size_bound(page_query: AsyncMock):
    response = await _request(_actor(), {"cursor": "a" * 1025})
    assert response.status_code == 422
    page_query.assert_not_awaited()


@pytest.mark.parametrize("params", [{"search": "new search"}, {"tenant_id": str(FOREIGN)}])
async def test_rejects_cursor_when_search_or_selected_tenant_changes(page_query: AsyncMock, params: dict[str, str]):
    response = await _request(_actor("platform_admin"), {"tenant_id": str(TENANT), "cursor": _cursor(), **params})
    assert response.status_code == 400
    page_query.assert_not_awaited()


@pytest.mark.parametrize("created_at", [NOW, None])
async def test_continues_when_cursor_matches_effective_scope(page_query: AsyncMock, created_at: datetime | None):
    token = _cursor(created_at=created_at.isoformat() if created_at else None)
    response = await _request(_actor(), {"cursor": token, "tenant_id": str(FOREIGN), "limit": 1})
    assert response.status_code == 200
    page_query.assert_awaited_once_with(TENANT, search="", after=UserPagePosition(0, created_at, UUID(int=10)), limit=1)


async def test_serializes_existing_public_fields_when_page_has_lookahead(
    page_query: AsyncMock, monkeypatch: pytest.MonkeyPatch
):
    identity = IdentityRecord(id=UUID(int=10), email="Alice@example.com", must_change_password=True)
    first = UserRecord(
        id=UUID(int=10),
        tenant_id=TENANT,
        identity=identity,
        display_name="Alice",
        role="org_admin",
        created_at=NOW,
        is_genesis=True,
        quota_message_limit=70,
        quota_messages_used=4,
        quota_max_agents=8,
    )
    second = UserRecord(id=UUID(int=11), tenant_id=TENANT, role="member", registration_source=None)
    page_query.return_value = [first, second, UserRecord(id=UUID(int=12))]
    counts = AsyncMock(return_value={first.id: 3})
    monkeypatch.setattr(users_api.agent_dao, "count_active_for_creators", counts)
    response = await _request(_actor(), {"limit": 2})
    assert response.status_code == 200
    page = users_api.UsersPageOut.model_validate_json(response.content)
    assert response.json()["items"] == [
        {
            "id": str(first.id),
            "tenant_id": str(TENANT),
            "email": "Alice@example.com",
            "display_name": "Alice",
            "role": "org_admin",
            "is_active": True,
            "is_genesis": True,
            "quota_message_limit": 70,
            "quota_message_period": "permanent",
            "quota_messages_used": 4,
            "quota_max_agents": 8,
            "quota_agent_ttl_hours": 0,
            "agents_count": 3,
            "created_at": NOW.isoformat(),
            "source": "web",
        },
        {
            "id": str(second.id),
            "tenant_id": str(TENANT),
            "email": "",
            "display_name": "User",
            "role": "member",
            "is_active": True,
            "is_genesis": False,
            "quota_message_limit": 50,
            "quota_message_period": "permanent",
            "quota_messages_used": 0,
            "quota_max_agents": 2,
            "quota_agent_ttl_hours": 0,
            "agents_count": 0,
            "created_at": None,
            "source": "registered",
        },
    ]
    counts.assert_awaited_once_with([first.id, second.id])
    token = page.next_cursor
    assert isinstance(token, str)
    position = users_api._UsersPageCursor.model_validate_json(urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    assert (position.role_rank, position.created_at, position.user_id) == (1, None, second.id)


async def test_returns_no_cursor_when_page_is_exactly_full(page_query: AsyncMock):
    page_query.return_value = [UserRecord(id=UUID(int=10))]
    response = await _request(_actor(), {"limit": 1})
    assert response.status_code == 200
    page = users_api.UsersPageOut.model_validate_json(response.content)
    assert len(page.items) == 1
    assert page.next_cursor is None


async def test_legacy_array_when_existing_endpoint_is_requested(page_query: AsyncMock, monkeypatch: pytest.MonkeyPatch):
    member = UserRecord(id=UUID(int=10), tenant_id=TENANT)
    legacy = AsyncMock(return_value=[member])
    count = AsyncMock(return_value=2)
    monkeypatch.setattr(users_api.user_dao, "list_for_tenant_ordered", legacy)
    monkeypatch.setattr(users_api.agent_dao, "count_active_for_creator", count)
    result = await users_api.list_users(tenant_id=str(FOREIGN), current_user=_actor())
    assert isinstance(result, list)
    assert [item.agents_count for item in result] == [2]
    legacy.assert_awaited_once_with(TENANT, include_identity=True)
    count.assert_awaited_once_with(member.id)
    page_query.assert_not_awaited()


@pytest.mark.parametrize(
    ("actor", "tenant_id", "status"),
    [
        (_actor("member"), str(TENANT), 403),
        (_actor(tenant_id=None), str(TENANT), 403),
        (_actor("platform_admin"), None, 400),
        (_actor("platform_admin"), "invalid", 400),
    ],
)
async def test_preserves_legacy_scope_errors_when_request_is_invalid(
    page_query: AsyncMock, actor: UserRecord, tenant_id: str | None, status: int
):
    with pytest.raises(HTTPException) as raised:
        await users_api.list_users(tenant_id=tenant_id, current_user=actor)
    assert raised.value.status_code == status
    page_query.assert_not_awaited()


async def test_password_change_gate_when_page_uses_real_auth_dependency(
    page_query: AsyncMock, monkeypatch: pytest.MonkeyPatch
):
    actor = UserRecord(
        id=UUID(int=1),
        tenant_id=TENANT,
        role="org_admin",
        identity=IdentityRecord(id=UUID(int=1), must_change_password=True),
    )
    monkeypatch.setattr(security, "decode_access_token", lambda _token: {"sub": str(actor.id)})
    monkeypatch.setattr(security.user_dao, "get_with_identity", AsyncMock(return_value=actor))
    app = FastAPI()
    app.include_router(users_api.router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/users/page", headers={"Authorization": "Bearer test-token"})
    assert response.status_code == 403
    page_query.assert_not_awaited()
