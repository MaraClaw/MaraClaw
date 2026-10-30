from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from tests.test_users_pagination_postgres import FOREIGN, TENANT, _insert

from app.api import organization
from app.core.domain.users import UserRole
from app.core.security import get_current_user
from app.db.connection import DbConnection
from app.records.identity import IdentityRecord
from app.records.user import UserRecord

pytest_plugins = ("tests.test_users_pagination_postgres",)
pytestmark = pytest.mark.usefixtures("database")


@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()
    app.include_router(organization.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: UserRecord(id=UUID(int=1), tenant_id=TENANT)
    return app


@pytest.fixture
async def client(database: DbConnection, app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.fixture
async def people(database: DbConnection) -> None:
    await database.execute(
        "CREATE TYPE pg_temp.directory_role AS ENUM ('platform_admin', 'org_admin', 'agent_admin', 'member')"
    )
    await database.execute(
        "ALTER TABLE users ALTER COLUMN role TYPE pg_temp.directory_role USING role::pg_temp.directory_role"
    )
    for number, name, tenant in [(3, "Same", TENANT), (1, "Same", TENANT), (2, "Same", TENANT), (4, "Same", FOREIGN)]:
        await _insert(
            database,
            UserRecord(
                id=UUID(int=number),
                tenant_id=tenant,
                display_name=name,
                role=UserRole.MEMBER,
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                identity=IdentityRecord(id=UUID(int=number), email=f"person{number}@example.com"),
            ),
        )
    await database.execute(
        "UPDATE users SET title = %(title)s WHERE id = %(id)s", {"title": "Lead%_\\", "id": UUID(int=2)}
    )
    await database.execute("UPDATE users SET is_active = false WHERE id = %(id)s", {"id": UUID(int=3)})


async def test_tied_names_page_in_id_order(client: httpx.AsyncClient, people: None) -> None:
    first = await client.get("/api/org/users/page", params={"limit": 1})
    assert first.status_code == 200
    second = await client.get("/api/org/users/page", params={"limit": 1, "cursor": first.json()["next_cursor"]})
    assert second.status_code == 200
    assert [first.json()["items"][0]["id"], second.json()["items"][0]["id"]] == [str(UUID(int=1)), str(UUID(int=2))]
    assert second.json()["next_cursor"] is None


@pytest.mark.parametrize("search", ["%", "_", "\\", "lead%_\\"])
async def test_search_treats_wildcards_as_literals(client: httpx.AsyncClient, people: None, search: str) -> None:
    response = await client.get("/api/org/users/page", params={"search": search})
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [str(UUID(int=2))]


async def test_search_matches_role_enum(client: httpx.AsyncClient, people: None) -> None:
    response = await client.get("/api/org/users/page", params={"search": "MEMBER"})
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [str(UUID(int=1)), str(UUID(int=2))]


async def test_member_foreign_selector_is_ignored(client: httpx.AsyncClient, people: None) -> None:
    response = await client.get("/api/org/users/page", params={"tenant_id": str(FOREIGN)})
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [str(UUID(int=1)), str(UUID(int=2))]


@pytest.mark.parametrize(("role", "status"), [(UserRole.ORG_ADMIN, 403), (UserRole.PLATFORM_ADMIN, 200)])
async def test_admin_foreign_selector(
    app: FastAPI, client: httpx.AsyncClient, people: None, role: UserRole, status: int
) -> None:
    app.dependency_overrides[get_current_user] = lambda: UserRecord(id=UUID(int=1), tenant_id=TENANT, role=role)
    response = await client.get("/api/org/users/page", params={"tenant_id": str(FOREIGN)})
    assert response.status_code == status
    if status == 200:
        assert [item["id"] for item in response.json()["items"]] == [str(UUID(int=4))]


async def test_cursor_rejects_different_search(client: httpx.AsyncClient, people: None) -> None:
    first = await client.get("/api/org/users/page", params={"limit": 1})
    response = await client.get(
        "/api/org/users/page", params={"cursor": first.json()["next_cursor"], "search": "other"}
    )
    assert response.status_code == 400
