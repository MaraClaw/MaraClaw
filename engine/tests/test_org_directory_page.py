from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI

from app.api import organization
from app.core.domain.users import UserRole
from app.core.security import get_current_user
from app.dao.user_dao import user_dao
from app.records.user import UserRecord

TENANT = UUID(int=100)
FOREIGN = UUID(int=200)


@pytest.mark.parametrize(
    ("role", "expected"),
    [(UserRole.MEMBER, TENANT), (UserRole.AGENT_ADMIN, TENANT), (UserRole.PLATFORM_ADMIN, FOREIGN)],
)
async def test_selector_scope_when_role_varies(monkeypatch: pytest.MonkeyPatch, role: UserRole, expected: UUID) -> None:
    listing = AsyncMock(return_value=[])
    monkeypatch.setattr(user_dao, "list_directory_page", listing)
    app = FastAPI()
    app.include_router(organization.router)
    app.dependency_overrides[get_current_user] = lambda: UserRecord(id=UUID(int=1), tenant_id=TENANT, role=role)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/org/users/page", params={"tenant_id": str(FOREIGN), "search": "  Alice  "})
    assert response.status_code == 200
    listing.assert_awaited_once_with(expected, search="Alice", after=None, limit=50)


@pytest.mark.parametrize(
    ("params", "status"),
    [({"tenant_id": str(FOREIGN)}, 403), ({"cursor": "bad!"}, 400), ({"limit": 0}, 422), ({"limit": 201}, 422)],
)
async def test_invalid_page_request(monkeypatch: pytest.MonkeyPatch, params: dict[str, str | int], status: int) -> None:
    monkeypatch.setattr(user_dao, "list_directory_page", AsyncMock(return_value=[]))
    app = FastAPI()
    app.include_router(organization.router)
    app.dependency_overrides[get_current_user] = lambda: UserRecord(
        id=UUID(int=1), tenant_id=TENANT, role=UserRole.ORG_ADMIN
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/org/users/page", params=params)
    assert response.status_code == status


async def test_cursor_rejected_for_other_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        UserRecord(id=UUID(int=n), tenant_id=TENANT, display_name="Name", created_at=datetime(2026, 1, 1, tzinfo=UTC))
        for n in (1, 2)
    ]
    monkeypatch.setattr(user_dao, "list_directory_page", AsyncMock(return_value=rows))
    app = FastAPI()
    app.include_router(organization.router)
    app.dependency_overrides[get_current_user] = lambda: UserRecord(id=UUID(int=3), role=UserRole.PLATFORM_ADMIN)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = await client.get("/org/users/page", params={"tenant_id": str(TENANT), "limit": 1})
        second = await client.get(
            "/org/users/page", params={"tenant_id": str(FOREIGN), "cursor": first.json()["next_cursor"]}
        )
    assert second.status_code == 400
