"""Tenant disable cascade: system orgs stay on; members/agents/automations turn off."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api import tenants as tenants_api
from app.core.domain.users import UserRole
from app.records.organization_plan import OrganizationPlanId
from app.records.tenant import TenantRecord
from app.records.user import UserRecord
from app.services import tenant_lifecycle as lifecycle
from app.services.org_membership import DefaultOrgUnavailableError
from app.services.tenant_lifecycle import (
    TenantActivationChange,
    change_tenant_active,
    set_tenant_active,
    tenant_can_be_disabled,
)

_NOW = datetime.now(UTC)


def _tenant(**kwargs) -> TenantRecord:
    defaults: dict[str, Any] = {
        "id": uuid4(),
        "name": "Acme",
        "slug": "acme",
        "tier": OrganizationPlanId.PRO,
        "is_active": True,
        "is_system": False,
        "is_default_end_user_org": False,
        "created_at": _NOW,
    }
    defaults.update(kwargs)
    return TenantRecord(**{k: defaults[k] for k in TenantRecord.__dataclass_fields__ if k in defaults})


@pytest.mark.parametrize("active", [False, True])
async def test_set_tenant_active_rechecks_locked_state_and_skips_a_retry(monkeypatch, active):
    current = _tenant(is_active=active)
    stale = _tenant(id=current.id, is_active=not active)
    db = SimpleNamespace(fetchone=AsyncMock(return_value=asdict(current)))

    @asynccontextmanager
    async def locked_context():
        yield db

    update = AsyncMock(return_value=current)
    deactivate = AsyncMock()
    reactivate = AsyncMock()
    monkeypatch.setattr(lifecycle, "connection_ctx", locked_context)
    monkeypatch.setattr(lifecycle.tenant_dao, "update", update)
    monkeypatch.setattr(lifecycle.user_dao, "deactivate_for_tenant", deactivate)
    monkeypatch.setattr(lifecycle.user_dao, "reactivate_for_tenant", reactivate)
    monkeypatch.setattr(lifecycle, "free_account_slots", AsyncMock(return_value=None))
    for dao in (lifecycle.agent_dao, lifecycle.agent_trigger_dao, lifecycle.agent_schedule_dao):
        monkeypatch.setattr(dao, "disable_for_tenant", AsyncMock())
    monkeypatch.setattr(lifecycle.agent_dao, "list_for_tenant", AsyncMock(return_value=[]))

    result = await set_tenant_active(stale, is_active=active)

    assert result.is_active is active
    update.assert_not_awaited()
    deactivate.assert_not_awaited()
    reactivate.assert_not_awaited()
    query, params = db.fetchone.await_args.args
    assert "FOR UPDATE" in query
    assert params == {"id": current.id}


@pytest.mark.asyncio
async def test_set_tenant_active_disables_members_agents_and_services(monkeypatch):
    tenant = _tenant()
    db = SimpleNamespace(
        fetchone=AsyncMock(side_effect=[asdict(tenant), asdict(_tenant(id=tenant.id, is_active=False))])
    )

    @asynccontextmanager
    async def context():
        yield db

    monkeypatch.setattr(lifecycle, "connection_ctx", context)
    monkeypatch.setattr(lifecycle.tenant_dao, "update", AsyncMock(side_effect=[_tenant(is_active=False), tenant]))
    monkeypatch.setattr(lifecycle, "free_account_slots", AsyncMock(return_value=4))
    monkeypatch.setattr(lifecycle.user_dao, "count_suspended_for_tenant", AsyncMock(return_value=6))
    deactivate = AsyncMock(return_value=4)
    disable_agents = AsyncMock(return_value=2)
    disable_triggers = AsyncMock(return_value=3)
    disable_schedules = AsyncMock(return_value=1)
    reactivate = AsyncMock(return_value=4)
    monkeypatch.setattr(lifecycle.user_dao, "deactivate_for_tenant", deactivate)
    monkeypatch.setattr(lifecycle.agent_dao, "disable_for_tenant", disable_agents)
    monkeypatch.setattr(lifecycle.agent_dao, "list_for_tenant", AsyncMock(return_value=[]))
    monkeypatch.setattr(lifecycle.agent_trigger_dao, "disable_for_tenant", disable_triggers)
    monkeypatch.setattr(lifecycle.agent_schedule_dao, "disable_for_tenant", disable_schedules)
    monkeypatch.setattr(lifecycle.user_dao, "reactivate_for_tenant", reactivate)

    await set_tenant_active(tenant, is_active=False)
    deactivate.assert_awaited_once_with(tenant.id)
    disable_agents.assert_awaited_once_with(tenant.id)
    disable_triggers.assert_awaited_once_with(tenant.id)
    disable_schedules.assert_awaited_once_with(tenant.id)
    reactivate.assert_not_awaited()

    result = await change_tenant_active(tenant, is_active=True)
    reactivate.assert_awaited_once_with(tenant.id, limit=4)
    assert result.restored_members == 4
    assert result.inactive_members == 2
    for disable in (deactivate, disable_agents, disable_triggers, disable_schedules):
        assert disable.await_count == 1


@pytest.mark.asyncio
async def test_set_tenant_active_refuses_system_orgs(monkeypatch):
    db = SimpleNamespace(
        fetchone=AsyncMock(side_effect=[asdict(_tenant(is_system=True)), asdict(_tenant(is_default_end_user_org=True))])
    )

    @asynccontextmanager
    async def context():
        yield db

    monkeypatch.setattr(lifecycle, "connection_ctx", context)
    with pytest.raises(DefaultOrgUnavailableError):
        await set_tenant_active(_tenant(is_system=True), is_active=False)
    with pytest.raises(DefaultOrgUnavailableError):
        await set_tenant_active(_tenant(is_default_end_user_org=True), is_active=False)


def test_tenant_can_be_disabled():
    assert tenant_can_be_disabled(_tenant()) is True
    assert tenant_can_be_disabled(_tenant(is_system=True)) is False
    assert tenant_can_be_disabled(_tenant(is_default_end_user_org=True)) is False
    assert tenant_can_be_disabled(SimpleNamespace(is_system=True, is_default_end_user_org=False)) is False


@pytest.mark.asyncio
async def test_update_tenant_is_active_uses_cascade(monkeypatch):
    tenant = _tenant()
    set_active = AsyncMock(return_value=TenantActivationChange(_tenant(id=tenant.id, is_active=False), changed=True))
    monkeypatch.setattr(tenants_api.tenant_dao, "get", AsyncMock(return_value=tenant))
    monkeypatch.setattr(tenants_api, "change_tenant_active", set_active)
    monkeypatch.setattr(tenants_api, "write_admin_audit", AsyncMock())
    actor: Any = SimpleNamespace(role="platform_admin", tenant_id=None, email="pa@x.com")
    await tenants_api.update_tenant(tenant.id, tenants_api.TenantUpdate(is_active=False), actor)
    set_active.assert_awaited_once()
    assert set_active.await_args is not None
    assert set_active.await_args.kwargs["is_active"] is False


@pytest.mark.asyncio
async def test_update_tenant_org_admin_cannot_set_is_active(monkeypatch):
    tenant = _tenant()
    set_active = AsyncMock(return_value=tenant)
    monkeypatch.setattr(tenants_api.tenant_dao, "get", AsyncMock(return_value=tenant))
    monkeypatch.setattr(tenants_api, "change_tenant_active", set_active)
    actor: Any = SimpleNamespace(
        role="org_admin",
        tenant_id=tenant.id,
        email="oa@x.com",
        is_platform_admin=False,
        identity=None,
    )
    with pytest.raises(HTTPException) as exc:
        await tenants_api.update_tenant(tenant.id, tenants_api.TenantUpdate(is_active=False), actor)
    assert exc.value.status_code == 403
    set_active.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_tenant_refuses_system_org(monkeypatch):
    tenant = _tenant(is_system=True)
    monkeypatch.setattr(tenants_api.tenant_dao, "get", AsyncMock(return_value=tenant))
    monkeypatch.setattr(
        tenants_api,
        "change_tenant_active",
        AsyncMock(side_effect=DefaultOrgUnavailableError("Cannot disable a system organization")),
    )
    actor: Any = SimpleNamespace(role="platform_admin", tenant_id=None, email="pa@x.com")
    with pytest.raises(HTTPException) as exc:
        await tenants_api.update_tenant(tenant.id, tenants_api.TenantUpdate(is_active=False), actor)
    assert exc.value.status_code == 400


async def test_update_tenant_active_retry_does_not_audit(monkeypatch):
    tenant = _tenant(is_active=False)
    monkeypatch.setattr(tenants_api.tenant_dao, "get", AsyncMock(return_value=tenant))
    monkeypatch.setattr(
        tenants_api, "change_tenant_active", AsyncMock(return_value=TenantActivationChange(tenant, changed=False))
    )
    audit = AsyncMock()
    monkeypatch.setattr(tenants_api, "write_admin_audit", audit)
    actor = UserRecord(id=uuid4(), role=UserRole.PLATFORM_ADMIN)

    result = await tenants_api.update_tenant(tenant.id, tenants_api.TenantUpdate(is_active=False), actor)

    assert result.is_active is False
    audit.assert_not_awaited()
