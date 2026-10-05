from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app.api import agents
from app.core import access_cache
from app.records.agent import AgentPermissionRecord, AgentRecord
from app.records.identity import IdentityRecord
from app.records.local_department import LocalDepartmentRecord
from app.records.user import UserRecord


@pytest.fixture
def directory(monkeypatch):
    tenant = uuid4()
    owner = UserRecord(id=uuid4(), tenant_id=tenant)
    agent = AgentRecord(id=uuid4(), name="Sales", creator_id=owner.id, tenant_id=tenant, access_mode="custom")
    department = LocalDepartmentRecord(id=uuid4(), tenant_id=tenant, name="Sales", created_at=datetime.now(UTC))
    grants = [AgentPermissionRecord(id=uuid4(), agent_id=agent.id, scope_type="department", scope_id=department.id)]

    async def list_grants(_id):
        return list(grants)

    async def create_grant(*, obj_in):
        row = AgentPermissionRecord(id=uuid4(), **obj_in)
        grants.append(row)
        return row

    async def delete_grants(_id):
        grants.clear()

    async def update_agent(*, db_obj, obj_in):
        return replace(db_obj, **obj_in)

    async def get_departments(ids):
        return [department] if department.id in ids else []

    async def get_users(ids):
        return [owner] if owner.id in ids else []

    monkeypatch.setattr(agents.agent_dao, "get", AsyncMock(return_value=agent))
    monkeypatch.setattr(agents.user_dao, "get", AsyncMock(return_value=owner))
    monkeypatch.setattr(agents.agent_dao, "update", update_agent)
    monkeypatch.setattr(agents.agent_permission_dao, "list_for_agent", list_grants)
    monkeypatch.setattr(agents.agent_permission_dao, "create", create_grant)
    monkeypatch.setattr(agents.agent_permission_dao, "delete_for_agent", delete_grants)
    monkeypatch.setattr(agents.local_department_dao, "get_many", get_departments)
    monkeypatch.setattr(agents.user_dao, "get_many", get_users)
    monkeypatch.setattr(agents.user_dao, "get_many_with_identity", AsyncMock(return_value={owner.id: owner}))
    monkeypatch.setattr(agents, "_get_active_admin_users", AsyncMock(return_value=[]))
    monkeypatch.setattr(agents, "ensure_access_granted_platform_relationships", AsyncMock(return_value=False))
    monkeypatch.setattr(access_cache, "get_cached_level", AsyncMock(return_value=None))
    monkeypatch.setattr(access_cache, "set_cached_level", AsyncMock())
    access_cache.clear_request_memo()
    return owner, agent, department, grants


@pytest.mark.asyncio
async def test_acl_write_rejects_fresh_actor_password_change_before_deletion(directory, monkeypatch):
    owner, agent, _, grants = directory
    before = list(grants)
    actor = replace(owner, identity=IdentityRecord(id=uuid4(), must_change_password=True))

    @asynccontextmanager
    async def transaction():
        yield object()

    monkeypatch.setattr(agents, "optional_connection_ctx", transaction)
    monkeypatch.setattr(agents, "lock_tenants", AsyncMock())
    monkeypatch.setattr(agents, "lock_user", AsyncMock())
    monkeypatch.setattr(agents, "lock_agent", AsyncMock())
    monkeypatch.setattr(agents.user_dao, "get_with_identity", AsyncMock(return_value=actor))
    with pytest.raises(HTTPException) as error:
        await agents.update_agent_permissions(agent.id, agents.AgentPermissionUpdate(scope_type="private"), owner)
    assert error.value.status_code == 403
    assert grants == before


@pytest.mark.asyncio
@pytest.mark.parametrize(("selection", "expected_count"), [("omitted", 1), ("empty", 0), ("selected", 1)])
async def test_custom_department_selection_roundtrip(directory, selection, expected_count):
    # Given an existing department grant.
    owner, agent, department, grants = directory
    selection_ids = None if selection == "omitted" else [] if selection == "empty" else [department.id]
    # When custom permissions are saved with an omitted/empty/explicit selection.
    result = await agents.update_agent_permissions(
        agent.id, agents.AgentPermissionUpdate(scope_type="custom", department_ids=selection_ids), owner
    )
    # Then the selection round trips, use-only, and the creator remains a manager.
    response = await agents.get_agent_permissions(agent.id, owner)
    assert result == {"status": "ok"}
    assert response["department_ids"] == ([str(department.id)] if expected_count else [])
    assert response["department_access"] == (
        [{"id": str(department.id), "name": "Sales", "access_level": "use"}] if expected_count else []
    )
    assert any(p.scope_id == owner.id and p.access_level == "manage" for p in grants)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["private", "company", "user"])
async def test_other_modes_reject_nonempty_department_selection_before_delete(directory, mode):
    owner, agent, department, grants = directory
    before = list(grants)
    with pytest.raises(HTTPException) as error:
        await agents.update_agent_permissions(
            agent.id, agents.AgentPermissionUpdate(scope_type=mode, department_ids=[department.id]), owner
        )
    assert error.value.status_code == 422
    assert grants == before


@pytest.mark.asyncio
async def test_unknown_department_rejects_before_deleting_existing_permissions(directory):
    owner, agent, _, grants = directory
    before = list(grants)
    with pytest.raises(HTTPException) as error:
        await agents.update_agent_permissions(
            agent.id, agents.AgentPermissionUpdate(scope_type="custom", department_ids=[uuid4()]), owner
        )
    assert error.value.status_code == 422
    assert grants == before


@pytest.mark.asyncio
async def test_permissions_empty_branch_has_department_fields(directory):
    owner, agent, _, grants = directory
    grants.clear()
    response = await agents.get_agent_permissions(agent.id, owner)
    assert response["department_ids"] == []
    assert response["department_access"] == []


@pytest.mark.asyncio
async def test_department_candidates_are_manager_only(directory, monkeypatch):
    owner, agent, _department, _ = directory
    member = UserRecord(id=uuid4(), tenant_id=owner.tenant_id)
    monkeypatch.setattr(agents, "check_agent_access", AsyncMock(return_value=(agent, "use")))
    with pytest.raises(HTTPException) as error:
        await agents.get_agent_permission_departments(agent.id, member)
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_department_candidates_are_agent_tenant_scoped(directory, monkeypatch):
    owner, agent, department, _ = directory

    async def for_tenant(tenant_id: UUID):
        return [department] if tenant_id == department.tenant_id else []

    monkeypatch.setattr(agents.local_department_dao, "list_for_tenant", for_tenant)
    assert await agents.get_agent_permission_departments(agent.id, owner) == {
        "departments": [{"id": str(department.id), "name": "Sales"}]
    }
