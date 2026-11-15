"""Activate or deactivate a tenant and its members / agents / automations."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException

from app.dao.agent_dao import agent_dao
from app.dao.schedule_dao import agent_schedule_dao
from app.dao.tenant_dao import tenant_dao
from app.dao.trigger_dao import agent_trigger_dao
from app.dao.user_dao import user_dao
from app.db.session import connection_ctx
from app.records.tenant import TenantRecord
from app.services.agent_stops import schedule_container_stops
from app.services.company_capacity import free_account_slots
from app.services.org_membership import assert_may_deactivate_tenant


@dataclass(frozen=True, slots=True)
class TenantActivationChange:
    tenant: TenantRecord
    changed: bool
    restored_members: int = 0
    inactive_members: int = 0


async def change_tenant_active(tenant: TenantRecord, *, is_active: bool) -> TenantActivationChange:
    """Apply a desired state under the tenant lock, restoring only suspension marks."""
    async with connection_ctx() as db:
        row = await db.fetchone("SELECT * FROM tenants WHERE id = %(id)s FOR UPDATE", {"id": tenant.id})
        if row is None:
            raise HTTPException(status_code=404, detail="Tenant not found")
        current = TenantRecord.from_row(row)
        if current.is_active == is_active:
            return TenantActivationChange(current, changed=False)
        if not is_active:
            assert_may_deactivate_tenant(current, making_active=False)

        updated = await tenant_dao.update(db_obj=current, obj_in={"is_active": is_active})
        tenant_id = current.id
        restored = inactive = 0
        if not is_active:
            _ = await user_dao.deactivate_for_tenant(tenant_id)
            _ = await agent_dao.disable_for_tenant(tenant_id)
            _ = await agent_trigger_dao.disable_for_tenant(tenant_id)
            _ = await agent_schedule_dao.disable_for_tenant(tenant_id)
            schedule_container_stops(await agent_dao.list_for_tenant(tenant_id))
        else:
            slots = await free_account_slots(db, tenant_id)
            marked = await user_dao.count_suspended_for_tenant(tenant_id)
            restored = await user_dao.reactivate_for_tenant(tenant_id, limit=slots)
            inactive = marked - restored
        return TenantActivationChange(updated, changed=True, restored_members=restored, inactive_members=inactive)


async def set_tenant_active(tenant: TenantRecord, *, is_active: bool) -> TenantRecord:
    """Compatibility entry point for callers that need only the resulting tenant."""
    return (await change_tenant_active(tenant, is_active=is_active)).tenant


def tenant_can_be_disabled(tenant: object) -> bool:
    return not bool(getattr(tenant, "is_system", False) or getattr(tenant, "is_default_end_user_org", False))
