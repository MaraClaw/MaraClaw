from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import HTTPException

from app.core import access_cache
from app.core.permissions import check_agent_access
from app.dao.org_department_dao import org_department_dao
from app.db.session import transaction
from app.records.user import UserRecord
from app.services import org_departments
from department_delete_postgres_fixtures import DepartmentStore, department_store, request
from test_bootstrap_postgres import schema_db

__all__ = ["department_store", "schema_db"]


async def test_delete_preserves_unrelated_grants_memberships_and_effective_access(
    department_store: DepartmentStore,
) -> None:
    # Given department-only, overlapping-department, direct and company access.
    store = department_store
    before = await store.db.fetchall(
        "SELECT id FROM agent_permissions WHERE scope_id IS DISTINCT FROM %(id)s", {"id": store.selected}
    )
    # When the real HTTP endpoint commits deletion.
    response = await request(store, "delete")
    # Then selected references disappear and unrelated access remains usable.
    assert response.status_code == 204
    assert response.content == b""
    assert await store.db.fetchall("SELECT id FROM local_departments ORDER BY id") == [{"id": store.other}]
    assert await store.db.fetchall("SELECT user_id, department_id FROM local_department_memberships") == [
        {"user_id": store.members[1], "department_id": store.other}
    ]
    assert {row["id"] for row in await store.db.fetchall("SELECT id FROM agent_permissions")} == {
        row["id"] for row in before
    }
    assert set(store.invalidated) == set(store.agents[:2])
    assert len(store.invalidated) == 2
    with pytest.raises(HTTPException) as denied:
        await check_agent_access(UserRecord(id=store.members[0]), store.agents[0], fresh=True)
    assert denied.value.status_code == 403
    for member in store.members[1:]:
        _, level = await check_agent_access(UserRecord(id=member), store.agents[0], fresh=True)
        assert level == "use"
    _, company_level = await check_agent_access(UserRecord(id=store.members[0]), store.agents[2], fresh=True)
    assert company_level == "use"


async def test_delete_defers_acl_invalidation_until_commit(department_store: DepartmentStore) -> None:
    # Given an owning transaction surrounding the service call.
    store = department_store
    async with transaction():
        # When cleanup has executed but the owner has not committed.
        await org_departments.delete_department(store.actor, store.selected, store.tenant)
        # Then external readers and invalidation still observe the pre-commit state.
        assert store.invalidated == []
        assert (
            await store.db.fetchone("SELECT id FROM local_departments WHERE id = %(id)s", {"id": store.selected})
            is not None
        )
    assert set(store.invalidated) == set(store.agents[:2])
    assert await store.db.fetchone("SELECT id FROM local_departments WHERE id = %(id)s", {"id": store.selected}) is None


@pytest.mark.parametrize("stage", ["cleanup", "acl"])
async def test_delete_failure_rolls_back_all_rows_and_pending_acl(
    department_store: DepartmentStore, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    # Given a failure after real cleanup or after an ACL bump has been queued.
    store = department_store
    original_cleanup = org_department_dao.delete_department
    original_bump = access_cache.bump_agent_acl_version
    before = await store.db.fetchall("SELECT id FROM agent_permissions ORDER BY id")

    async def fail_cleanup(department) -> None:
        await original_cleanup(department)
        raise RuntimeError("injected cleanup failure")

    async def fail_bump(agent_id: UUID) -> None:
        await original_bump(agent_id)
        raise RuntimeError("injected ACL failure")

    if stage == "cleanup":
        monkeypatch.setattr(org_department_dao, "delete_department", fail_cleanup)
    else:
        monkeypatch.setattr(org_departments, "bump_agent_acl_version", fail_bump)
    # When the endpoint encounters the injected failure.
    response = await request(store, "delete")
    # Then the SQL transaction and queued invalidations both roll back.
    assert response.status_code == 500
    assert (
        await store.db.fetchone("SELECT id FROM local_departments WHERE id = %(id)s", {"id": store.selected})
        is not None
    )
    assert len(await store.db.fetchall("SELECT user_id FROM local_department_memberships")) == 4
    assert await store.db.fetchall("SELECT id FROM agent_permissions ORDER BY id") == before
    assert store.invalidated == []


@pytest.mark.parametrize("change", ["role", "tenant", "membership", "identity", "password"])
async def test_delete_uses_persisted_authority_not_http_principal(
    department_store: DepartmentStore, change: str
) -> None:
    # Given a stale dependency principal and changed persisted authority.
    store = department_store
    match change:
        case "role":
            await store.db.execute("UPDATE users SET role = 'member' WHERE id = %(id)s", {"id": store.actor.id})
        case "tenant":
            await store.db.execute(
                "UPDATE users SET tenant_id = %(tenant)s WHERE id = %(id)s",
                {"id": store.actor.id, "tenant": store.foreign},
            )
        case "membership":
            await store.db.execute("UPDATE users SET is_active = false WHERE id = %(id)s", {"id": store.actor.id})
        case "identity":
            await store.db.execute("UPDATE identities SET is_active = false WHERE id = %(id)s", {"id": store.identity})
        case "password":
            await store.db.execute(
                "UPDATE identities SET must_change_password = true WHERE id = %(id)s", {"id": store.identity}
            )
    # When deletion attempts to use the stale role or identity state.
    response = await request(store, "delete")
    # Then no cleanup or invalidation occurs.
    assert response.status_code == (409 if change == "tenant" else 403)
    assert (
        await store.db.fetchone("SELECT id FROM local_departments WHERE id = %(id)s", {"id": store.selected})
        is not None
    )
    assert len(await store.db.fetchall("SELECT user_id FROM local_department_memberships")) == 4
    assert len(await store.db.fetchall("SELECT id FROM agent_permissions")) == 5
    assert store.invalidated == []


async def test_permission_replacement_rejects_cached_manage_after_role_revocation(
    department_store: DepartmentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a stale cached manager decision for an administrator who is no longer an owner or admin.
    store = department_store
    await store.db.execute("UPDATE users SET role = 'member' WHERE id = %(id)s", {"id": store.actor.id})
    await store.db.execute(
        "UPDATE agents SET creator_id = %(id)s WHERE id = %(agent)s", {"id": store.members[2], "agent": store.agents[0]}
    )
    from app.dao import agent_dao

    agent = await agent_dao.get(store.agents[0])
    assert agent is not None
    cached = AsyncMock(return_value="manage")
    monkeypatch.setattr(access_cache, "get_cached_level", cached)
    # When replacing permissions after obtaining the tenant and agent locks.
    response = await request(store, "permission")
    # Then the cached manage decision cannot authorize the write.
    assert response.status_code == 403
    assert len(await store.db.fetchall("SELECT id FROM agent_permissions")) == 5
