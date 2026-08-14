"""Activate or deactivate a tenant and its members / agents / automations."""

from __future__ import annotations

from app.dao.agent_dao import agent_dao
from app.dao.schedule_dao import agent_schedule_dao
from app.dao.tenant_dao import tenant_dao
from app.dao.trigger_dao import agent_trigger_dao
from app.dao.user_dao import user_dao
from app.db.session import connection_ctx
from app.records.tenant import TenantRecord
from app.services.org_membership import assert_may_deactivate_tenant


async def set_tenant_active(tenant: TenantRecord, *, is_active: bool) -> TenantRecord:
    """Flip ``tenants.is_active`` and cascade disable when turning the org off.

    Disable: members (not platform_admin) go inactive; agents stop; triggers
    and schedules turn off. Enable: members are restored; agents/automations
    stay stopped so they do not wake unattended.
    """
    if not is_active:
        assert_may_deactivate_tenant(tenant, making_active=False)

    async with connection_ctx():
        updated = await tenant_dao.update(db_obj=tenant, obj_in={"is_active": is_active})
        tenant_id = tenant.id
        if not is_active:
            _ = await user_dao.deactivate_for_tenant(tenant_id)
            _ = await agent_dao.disable_for_tenant(tenant_id)
            _ = await agent_trigger_dao.disable_for_tenant(tenant_id)
            _ = await agent_schedule_dao.disable_for_tenant(tenant_id)
        else:
            _ = await user_dao.reactivate_for_tenant(tenant_id)
        return updated or tenant


def tenant_can_be_disabled(tenant: object) -> bool:
    return not bool(getattr(tenant, "is_system", False) or getattr(tenant, "is_default_end_user_org", False))
