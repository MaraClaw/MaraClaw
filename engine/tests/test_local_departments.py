"""Local department boundaries and dynamic access regressions."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.core import access_cache, permissions
from app.records.agent import AgentPermissionRecord, AgentRecord
from app.records.user import UserRecord
from app.services.local_departments import admin_tenant


def test_bootstrap_resolves_engine_baseline():
    from app.scripts.bootstrap_db import SCHEMA_BASELINE

    assert SCHEMA_BASELINE.is_file()


def test_department_name_is_trimmed_and_nonempty():
    from app.schemas.department import DepartmentCreate

    assert DepartmentCreate(name="  Sales  ").name == "Sales"
    with pytest.raises(ValidationError):
        DepartmentCreate(name="   ")


@pytest.mark.asyncio
async def test_department_grant_is_dynamic_use(monkeypatch):
    from app.dao.local_department_dao import local_department_dao

    tenant = uuid4()
    user = UserRecord(id=uuid4(), tenant_id=tenant)
    agent = AgentRecord(id=uuid4(), name="Sales", tenant_id=tenant, creator_id=uuid4(), access_mode="custom")
    monkeypatch.setattr(permissions.agent_dao, "get", AsyncMock(return_value=agent))
    monkeypatch.setattr(permissions.user_dao, "get", AsyncMock(return_value=user))
    monkeypatch.setattr(permissions.agent_permission_dao, "list_for_agent", AsyncMock(return_value=[]))
    monkeypatch.setattr(local_department_dao, "has_agent_access", AsyncMock(return_value=True))
    monkeypatch.setattr(access_cache, "get_cached_level", AsyncMock(return_value=None))
    monkeypatch.setattr(access_cache, "set_cached_level", AsyncMock())
    access_cache.clear_request_memo()
    assert (await permissions.check_agent_access(user, agent.id))[1] == "use"
    access_cache.clear_request_memo()
    monkeypatch.setattr(local_department_dao, "has_agent_access", AsyncMock(return_value=False))
    with pytest.raises(HTTPException) as error:
        await permissions.check_agent_access(user, agent.id)
    assert error.value.status_code == 403


@pytest.mark.parametrize(
    ("role", "selected", "expected"),
    [
        ("member", "own", 403),
        ("agent_admin", "own", 403),
        ("org_admin", "foreign", 403),
        ("platform_admin", "absent", 422),
    ],
)
def test_department_administration_denies_wrong_role_or_tenant(role, selected, expected):
    # Given an actor and a selected organization.
    tenant = uuid4()
    actor = UserRecord(id=uuid4(), tenant_id=tenant, role=role)
    target = tenant if selected == "own" else uuid4() if selected == "foreign" else None
    # When the directory boundary resolves the organization.
    with pytest.raises(HTTPException) as error:
        admin_tenant(actor, target)
    # Then unauthorized roles/selections are rejected before persistence.
    assert error.value.status_code == expected


@pytest.mark.parametrize("role", ["org_admin", "platform_admin"])
def test_department_administration_accepts_authorized_selection(role):
    tenant = uuid4()
    actor = UserRecord(id=uuid4(), tenant_id=tenant, role=role)
    assert admin_tenant(actor, tenant) == tenant


@pytest.mark.asyncio
async def test_direct_manage_beats_department_use(monkeypatch):
    tenant = uuid4()
    user = UserRecord(id=uuid4(), tenant_id=tenant)
    agent = AgentRecord(id=uuid4(), name="Sales", tenant_id=tenant, creator_id=uuid4(), access_mode="custom")
    grant = AgentPermissionRecord(
        id=uuid4(), agent_id=agent.id, scope_type="user", scope_id=user.id, access_level="manage"
    )
    monkeypatch.setattr(permissions.agent_dao, "get", AsyncMock(return_value=agent))
    monkeypatch.setattr(permissions.user_dao, "get", AsyncMock(return_value=user))
    monkeypatch.setattr(permissions.agent_permission_dao, "list_for_agent", AsyncMock(return_value=[grant]))
    monkeypatch.setattr(permissions.local_department_dao, "has_agent_access", AsyncMock(return_value=True))
    access_cache.clear_request_memo()
    assert (await permissions.check_agent_access(user, agent.id, fresh=True))[1] == "manage"


@pytest.mark.asyncio
async def test_accessible_relationship_users_filter_inactive_and_foreign_ids(monkeypatch):
    tenant = uuid4()
    creator, direct, department, inactive, foreign, admin = [uuid4() for _ in range(6)]
    agent = AgentRecord(id=uuid4(), name="Sales", creator_id=creator, tenant_id=tenant, access_mode="custom")
    grants = [
        AgentPermissionRecord(id=uuid4(), agent_id=agent.id, scope_type="user", scope_id=uid)
        for uid in (direct, inactive, foreign)
    ]
    monkeypatch.setattr(
        permissions.user_dao, "list_active_ids_for_tenant", AsyncMock(return_value=[creator, direct, department, admin])
    )
    monkeypatch.setattr(permissions.user_dao, "list_active_admin_ids_for_tenant", AsyncMock(return_value=[admin]))
    monkeypatch.setattr(permissions.agent_permission_dao, "list_for_agent", AsyncMock(return_value=grants))
    monkeypatch.setattr(
        permissions.agent_permission_dao, "list_user_scope_ids", AsyncMock(return_value=[direct, inactive, foreign])
    )
    monkeypatch.setattr(permissions.local_department_dao, "accessible_user_ids", AsyncMock(return_value={department}))
    assert await permissions.get_agent_accessible_user_ids(None, agent) == {creator, direct, department, admin}
