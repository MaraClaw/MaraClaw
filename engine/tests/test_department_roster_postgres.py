from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal, assert_never
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from psycopg import sql

from app.api import departments
from app.core.security import get_current_user
from app.dao.local_department_dao import local_department_dao
from app.db.connection import DbConnection
from app.records.user import UserRecord
from app.scripts import bootstrap_db
from app.services import local_departments
from test_bootstrap_postgres import schema_db as schema_db


@dataclass(frozen=True, slots=True)
class RosterStore:
    db: DbConnection
    tenant_id: UUID
    department_id: UUID
    membership_id: UUID
    identity_id: UUID


@pytest.fixture
async def roster_store(schema_db: DbConnection, monkeypatch: pytest.MonkeyPatch) -> RosterStore:
    await bootstrap_db.main()
    tenant, department, membership, identity = uuid4(), uuid4(), UUID(int=2), uuid4()
    await schema_db.execute("INSERT INTO tenants (id, name, slug) VALUES (%s, 'Acme', 'acme')", (tenant,))
    await schema_db.execute(
        "INSERT INTO local_departments (id, tenant_id, name) VALUES (%s, %s, 'Sales')", (department, tenant)
    )
    await schema_db.execute(
        "INSERT INTO identities (id, username, email) VALUES (%s, 'private-login', 'private@example.test')", (identity,)
    )
    await schema_db.execute(
        "INSERT INTO users (id, tenant_id, identity_id, display_name) VALUES (%s, %s, %s, 'Same Name')",
        (membership, tenant, identity),
    )
    await schema_db.execute(
        "INSERT INTO local_department_memberships (user_id, tenant_id, department_id) VALUES (%s, %s, %s)",
        (membership, tenant, department),
    )

    @asynccontextmanager
    async def session() -> AsyncGenerator[DbConnection]:
        yield schema_db

    monkeypatch.setattr(local_department_dao, "session", session)
    return RosterStore(schema_db, tenant, department, membership, identity)


async def read_roster(store: RosterStore, department_id: UUID) -> Response:
    app = FastAPI()
    app.include_router(departments.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: UserRecord(id=uuid4(), tenant_id=store.tenant_id)
    try:
        async with store.db.raw.transaction():
            await store.db.execute("SET TRANSACTION READ ONLY")
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                return await client.get(f"/api/departments/{department_id}/members")
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    ("role", "active", "identity_active", "platform", "included"),
    [
        ("member", True, True, False, True),
        ("agent_admin", True, True, False, True),
        ("org_admin", True, True, False, False),
        ("platform_admin", True, True, False, False),
        ("member", False, True, False, False),
        ("agent_admin", False, True, False, False),
        ("member", True, False, False, False),
        ("agent_admin", True, True, True, False),
    ],
)
async def test_roster_filters_membership_and_identity_policy(
    roster_store: RosterStore, role: str, active: bool, identity_active: bool, platform: bool, included: bool
) -> None:
    store = roster_store
    await store.db.execute(
        "UPDATE users SET role = %s, is_active = %s WHERE id = %s", (role, active, store.membership_id)
    )
    await store.db.execute(
        "UPDATE identities SET is_active = %s, is_platform_admin = %s WHERE id = %s",
        (identity_active, platform, store.identity_id),
    )

    response = await read_roster(store, store.department_id)

    assert response.status_code == 200
    assert response.json() == {
        "members": [{"membership_id": str(store.membership_id), "display_name": "Same Name"}] if included else []
    }


async def test_roster_preserves_blank_names_without_identity_fallback(roster_store: RosterStore) -> None:
    store = roster_store
    await store.db.execute("UPDATE users SET display_name = '' WHERE id = %s", (store.membership_id,))

    response = await read_roster(store, store.department_id)

    assert response.status_code == 200
    assert response.json() == {"members": [{"membership_id": str(store.membership_id), "display_name": ""}]}


async def test_roster_includes_active_membership_without_linked_identity(roster_store: RosterStore) -> None:
    store = roster_store
    await store.db.execute("UPDATE users SET identity_id = NULL WHERE id = %s", (store.membership_id,))

    response = await read_roster(store, store.department_id)

    assert response.status_code == 200
    assert response.json() == {"members": [{"membership_id": str(store.membership_id), "display_name": "Same Name"}]}


@pytest.mark.parametrize("contamination", ["user_tenant", "assignment_tenant", "department_tenant"])
async def test_roster_excludes_tenant_contaminated_assignments(
    roster_store: RosterStore, contamination: Literal["user_tenant", "assignment_tenant", "department_tenant"]
) -> None:
    store = roster_store
    foreign = uuid4()
    await store.db.execute("INSERT INTO tenants (id, name, slug) VALUES (%s, 'Foreign', 'foreign')", (foreign,))
    constraints = await store.db.fetchall(
        "SELECT conname FROM pg_constraint WHERE conrelid = 'local_department_memberships'::regclass AND contype = 'f'"
    )
    for constraint in constraints:
        await store.db.raw.execute(
            sql.SQL("ALTER TABLE local_department_memberships DROP CONSTRAINT {}").format(
                sql.Identifier(str(constraint["conname"]))
            )
        )
    match contamination:
        case "user_tenant":
            await store.db.execute("UPDATE users SET tenant_id = %s WHERE id = %s", (foreign, store.membership_id))
        case "assignment_tenant":
            await store.db.execute("UPDATE local_department_memberships SET tenant_id = %s", (foreign,))
        case "department_tenant":
            await store.db.execute("UPDATE users SET tenant_id = %s WHERE id = %s", (foreign, store.membership_id))
            await store.db.execute("UPDATE local_department_memberships SET tenant_id = %s", (foreign,))
        case unexpected:
            assert_never(unexpected)

    response = await read_roster(store, store.department_id)

    assert response.status_code == 200
    assert response.json() == {"members": []}


@pytest.mark.parametrize("second_department", [False, True])
async def test_roster_orders_duplicate_names_by_membership_id_across_multiple_departments(
    roster_store: RosterStore, second_department: bool
) -> None:
    store = roster_store
    other_department = uuid4()
    await store.db.execute(
        "INSERT INTO local_departments (id, tenant_id, name) VALUES (%s, %s, 'Support')",
        (other_department, store.tenant_id),
    )
    await store.db.execute(
        "INSERT INTO local_department_memberships (user_id, tenant_id, department_id) VALUES (%s, %s, %s)",
        (store.membership_id, store.tenant_id, other_department),
    )
    for membership, name in [(UUID(int=1), "Same Name"), (UUID(int=3), "Aaron"), (UUID(int=4), "Zoe")]:
        await store.db.execute(
            "INSERT INTO users (id, tenant_id, display_name) VALUES (%s, %s, %s)", (membership, store.tenant_id, name)
        )
        await store.db.execute(
            "INSERT INTO local_department_memberships (user_id, tenant_id, department_id) VALUES (%s, %s, %s)",
            (membership, store.tenant_id, store.department_id),
        )

    response = await read_roster(store, other_department if second_department else store.department_id)

    assert response.status_code == 200
    assert response.json() == {
        "members": [{"membership_id": str(store.membership_id), "display_name": "Same Name"}]
        if second_department
        else [
            {"membership_id": str(UUID(int=3)), "display_name": "Aaron"},
            {"membership_id": str(UUID(int=1)), "display_name": "Same Name"},
            {"membership_id": str(store.membership_id), "display_name": "Same Name"},
            {"membership_id": str(UUID(int=4)), "display_name": "Zoe"},
        ]
    }


async def test_roster_reads_do_not_resolve_grants_or_bump_acl(
    roster_store: RosterStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = roster_store
    forbidden = AsyncMock(side_effect=AssertionError("Roster must not change access or assignments"))
    for name in ("replace_assignments", "grant_agents_for_user"):
        monkeypatch.setattr(local_department_dao, name, forbidden)
    monkeypatch.setattr(local_departments, "bump_agent_acl_version", forbidden)

    response = await read_roster(store, store.department_id)

    assert response.status_code == 200
    forbidden.assert_not_awaited()


@pytest.mark.parametrize("selection", ["empty", "missing", "foreign"])
async def test_roster_distinguishes_empty_from_missing_or_foreign(roster_store: RosterStore, selection: str) -> None:
    store = roster_store
    selected = uuid4()
    if selection != "missing":
        tenant = store.tenant_id if selection == "empty" else uuid4()
        if selection == "foreign":
            await store.db.execute("INSERT INTO tenants (id, name, slug) VALUES (%s, 'Foreign', 'foreign')", (tenant,))
        await store.db.execute(
            "INSERT INTO local_departments (id, tenant_id, name) VALUES (%s, %s, 'Empty')", (selected, tenant)
        )

    response = await read_roster(store, selected)

    assert response.status_code == (200 if selection == "empty" else 404)
    assert response.json() == ({"members": []} if selection == "empty" else {"detail": "Department not found"})
