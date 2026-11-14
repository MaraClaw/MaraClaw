"""Activate or deactivate a tenant and its members / agents / automations."""

from __future__ import annotations

from typing import cast

from app.core.logging import logger
from app.dao.agent_dao import agent_dao
from app.dao.schedule_dao import agent_schedule_dao
from app.dao.tenant_dao import tenant_dao
from app.dao.trigger_dao import agent_trigger_dao
from app.dao.user_dao import user_dao
from app.db.connection import DbConnection
from app.db.session import connection_ctx
from app.records.tenant import TenantRecord
from app.services.agent_stops import schedule_container_stops
from app.services.company_capacity import free_account_slots
from app.services.org_membership import assert_may_deactivate_tenant


async def set_tenant_active(tenant: TenantRecord, *, is_active: bool) -> TenantRecord:
    """Flip ``tenants.is_active`` and cascade disable when turning the org off.

    Disable: members (not platform_admin) go inactive; agents stop; triggers
    and schedules turn off. Enable: members are restored, but a capped company
    gets back only as many accounts as it has free slots (organization admins
    first, see ``UserDAO.reactivate_for_tenant``); agents/automations stay
    stopped so they do not wake unattended.
    """
    if not is_active:
        assert_may_deactivate_tenant(tenant, making_active=False)

    async with connection_ctx() as connection:
        updated = await tenant_dao.update(db_obj=tenant, obj_in={"is_active": is_active})
        tenant_id = tenant.id
        if not is_active:
            _ = await user_dao.deactivate_for_tenant(tenant_id)
            _ = await agent_dao.disable_for_tenant(tenant_id)
            _ = await agent_trigger_dao.disable_for_tenant(tenant_id)
            _ = await agent_schedule_dao.disable_for_tenant(tenant_id)
            schedule_container_stops(await agent_dao.list_for_tenant(tenant_id))
        else:
            db = cast("DbConnection | None", connection)
            slots = await free_account_slots(db, tenant_id) if db is not None else None
            restored = await user_dao.reactivate_for_tenant(tenant_id, limit=slots)
            if slots is not None:
                logger.info(
                    "[tenant] Re-enabled capped company %s: %s free account slots, %s accounts restored",
                    tenant_id,
                    slots,
                    restored,
                )
        return updated or tenant


def tenant_can_be_disabled(tenant: object) -> bool:
    return not bool(getattr(tenant, "is_system", False) or getattr(tenant, "is_default_end_user_org", False))
