from collections.abc import AsyncIterator
from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import departments
from app.core.domain.users import UserRole
from app.core.security import get_current_user
from app.db.session import bind_crud_connection, transaction
from department_delete_postgres_fixtures import DepartmentStore, department_store, request
from test_bootstrap_postgres import schema_db

__all__ = ["department_store", "schema_db"]


@pytest.fixture
async def preview_client(department_store: DepartmentStore) -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    app.include_router(departments.router, prefix="/api", dependencies=[Depends(bind_crud_connection)])
    app.dependency_overrides[get_current_user] = lambda: department_store.actor
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.clear()


async def test_preview_counts_associations_despite_overlapping_access(
    department_store: DepartmentStore, preview_client: AsyncClient
) -> None:
    # Given three memberships, two department grants and overlapping access paths.
    store = department_store
    await store.db.execute("UPDATE users SET is_active = false WHERE id = %(id)s", {"id": store.members[0]})
    # When the administrator previews deletion without an explicit own-tenant override.
    response = await preview_client.get(f"/api/departments/{store.selected}/deletion-preview")
    # Then inactive memberships count too, without multiplying grants by members.
    assert response.status_code == 200
    assert response.json() == {"membership_count": 3, "agent_grant_count": 2}


async def test_preview_requires_authenticated_principal() -> None:
    app = FastAPI()
    app.include_router(departments.router, prefix="/api")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/departments/{uuid4()}/deletion-preview")
    assert response.status_code == 401


@pytest.mark.parametrize("query", ["?tenant_id=invalid", ""])
async def test_preview_rejects_invalid_uuid_inputs(preview_client: AsyncClient, query: str) -> None:
    department = uuid4() if query else "invalid"
    response = await preview_client.get(f"/api/departments/{department}/deletion-preview{query}")
    assert response.status_code == 422


async def test_preview_excludes_foreign_agent_associations(
    department_store: DepartmentStore, preview_client: AsyncClient
) -> None:
    store = department_store
    foreign_agent = uuid4()
    await store.db.execute(
        "INSERT INTO agents (id, tenant_id, creator_id, name, access_mode, role_description, max_triggers, "
        "min_poll_interval_min, webhook_rate_limit, company_access_level, heartbeat_interval_minutes) "
        "SELECT %(id)s, %(tenant)s, creator_id, 'Foreign', access_mode, role_description, max_triggers, "
        "min_poll_interval_min, webhook_rate_limit, company_access_level, heartbeat_interval_minutes "
        "FROM agents WHERE id = %(source)s",
        {"id": foreign_agent, "tenant": store.foreign, "source": store.agents[0]},
    )
    await store.db.execute(
        "INSERT INTO agent_permissions (id, agent_id, scope_type, scope_id, access_level) "
        "VALUES (%(id)s, %(agent)s, 'department', %(department)s, 'use')",
        {"id": uuid4(), "agent": foreign_agent, "department": store.selected},
    )
    response = await preview_client.get(f"/api/departments/{store.selected}/deletion-preview")
    assert response.status_code == 200
    assert response.json() == {"membership_count": 3, "agent_grant_count": 2}


async def test_preview_empty_department_returns_zero_counts(
    department_store: DepartmentStore, preview_client: AsyncClient
) -> None:
    # Given an existing department without associations.
    store = department_store
    empty = uuid4()
    await store.db.execute(
        "INSERT INTO local_departments (id, tenant_id, name) VALUES (%(id)s, %(tenant)s, 'Empty')",
        {"id": empty, "tenant": store.tenant},
    )
    # When previewing it.
    response = await preview_client.get(f"/api/departments/{empty}/deletion-preview?tenant_id={store.tenant}")
    # Then zero is a successful preview, not a missing department.
    assert response.status_code == 200
    assert response.json() == {"membership_count": 0, "agent_grant_count": 0}


async def test_preview_is_read_only_without_audit_or_acl_invalidation(
    department_store: DepartmentStore, preview_client: AsyncClient
) -> None:
    # Given a database transaction that rejects writes and row locks.
    store = department_store
    async with transaction() as db:
        await db.execute("SET TRANSACTION READ ONLY")
        # When the real HTTP route runs within that transaction.
        response = await preview_client.get(f"/api/departments/{store.selected}/deletion-preview")
    # Then PostgreSQL accepted the read and no post-commit invalidations or audit appeared.
    assert response.status_code == 200
    assert response.json() == {"membership_count": 3, "agent_grant_count": 2}
    assert await store.db.fetchall("SELECT id FROM admin_audit_logs") == []
    assert store.invalidated == []


@pytest.mark.parametrize(
    ("role", "selection", "status"),
    [
        (UserRole.MEMBER, "own", 403),
        (UserRole.AGENT_ADMIN, "own", 403),
        (UserRole.PLATFORM_ADMIN, "none", 422),
        (UserRole.ORG_ADMIN, "foreign", 403),
        (UserRole.PLATFORM_ADMIN, "foreign", 404),
        (UserRole.PLATFORM_ADMIN, "missing", 404),
    ],
)
async def test_preview_enforces_admin_tenant_selection(
    department_store: DepartmentStore, preview_client: AsyncClient, role: UserRole, selection: str, status: int
) -> None:
    # Given a principal and tenant selection at an authorization boundary.
    store = department_store
    store.actor.role = role
    selected = {"own": store.tenant, "foreign": store.foreign, "missing": uuid4(), "none": None}[selection]
    query = "" if selected is None else f"?tenant_id={selected}"
    # When requesting the known department through that boundary.
    response = await preview_client.get(f"/api/departments/{store.selected}/deletion-preview{query}")
    # Then no impact information or invalidation escapes.
    assert response.status_code == status
    assert "membership_count" not in response.json()
    assert "agent_grant_count" not in response.json()
    assert store.invalidated == []


async def test_platform_admin_preview_uses_explicit_target_not_membership_tenant(
    department_store: DepartmentStore, preview_client: AsyncClient
) -> None:
    # Given a platform administrator whose membership belongs to another organization.
    store = department_store
    store.actor.role = UserRole.PLATFORM_ADMIN
    store.actor.tenant_id = store.foreign
    # When explicitly selecting the department's organization.
    response = await preview_client.get(f"/api/departments/{store.selected}/deletion-preview?tenant_id={store.tenant}")
    # Then the selected organization's counts are returned.
    assert response.status_code == 200
    assert response.json() == {"membership_count": 3, "agent_grant_count": 2}


async def test_foreign_and_missing_departments_are_indistinguishable(
    department_store: DepartmentStore, preview_client: AsyncClient
) -> None:
    # Given an existing foreign department and an unknown department.
    store = department_store
    foreign_department = uuid4()
    await store.db.execute(
        "INSERT INTO local_departments (id, tenant_id, name) VALUES (%(id)s, %(tenant)s, 'Hidden')",
        {"id": foreign_department, "tenant": store.foreign},
    )
    # When requesting both from the administrator's own tenant.
    responses = [
        await preview_client.get(f"/api/departments/{department}/deletion-preview")
        for department in (foreign_department, uuid4())
    ]
    # Then both return the same non-disclosing error.
    assert [(response.status_code, response.json()) for response in responses] == [
        (404, {"detail": "Department not found"}),
        (404, {"detail": "Department not found"}),
    ]


async def test_delete_after_preview_rechecks_changed_associations(
    department_store: DepartmentStore, preview_client: AsyncClient
) -> None:
    # Given a completed preview followed by a newly added grant association.
    store = department_store
    preview = await preview_client.get(f"/api/departments/{store.selected}/deletion-preview")
    assert preview.json() == {"membership_count": 3, "agent_grant_count": 2}
    await store.db.execute(
        "INSERT INTO agent_permissions (id, agent_id, scope_type, scope_id, access_level) "
        "VALUES (%(id)s, %(agent)s, 'department', %(department)s, 'use')",
        {"id": uuid4(), "agent": store.agents[2], "department": store.selected},
    )
    # When deleting without supplying or refreshing preview data.
    response = await request(store, "delete")
    # Then cleanup and ACL invalidation use the current grants, including the new one.
    assert response.status_code == 204
    assert set(store.invalidated) == set(store.agents)
    assert (
        await store.db.fetchall(
            "SELECT id FROM agent_permissions WHERE scope_type = 'department' AND scope_id = %(id)s",
            {"id": store.selected},
        )
        == []
    )


async def test_delete_after_preview_rechecks_persisted_authority(
    department_store: DepartmentStore, preview_client: AsyncClient
) -> None:
    # Given a successful preview followed by persisted role revocation.
    store = department_store
    stale = replace(store.actor)
    preview = await preview_client.get(f"/api/departments/{store.selected}/deletion-preview")
    assert preview.status_code == 200
    await store.db.execute("UPDATE users SET role = 'member' WHERE id = %(id)s", {"id": store.actor.id})
    # When deletion is attempted with the still-authorized HTTP principal.
    response = await request(replace(store, actor=stale), "delete")
    # Then the earlier preview grants no authority to delete.
    assert response.status_code == 403
    assert await store.db.fetchval("SELECT count(*) FROM local_department_memberships") == 4
    assert await store.db.fetchval("SELECT count(*) FROM agent_permissions") == 5
    assert store.invalidated == []
