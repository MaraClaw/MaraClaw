from collections.abc import Sequence
from uuid import UUID

from app.dao.agent_storage_dao import agent_storage_dao as dao
from app.db.session import connection_ctx
from app.services.organization_plans import plan_for, tier_for_tenant
from app.services.storage_runtime.quota_errors import StorageTransferBlockedError


async def require_destination_storage(target_tenant_id: UUID | None, agent_ids: Sequence[UUID]) -> None:
    """Refuse a paid company transfer whose agents already hold more than the destination tier allows.

    Locks each agent's usage row inside the transfer's own transaction (the join is deliberate), so a writer that
    reserved against the old tier has already committed and is counted here, and a later writer waits until the
    agent's organization has changed and then reserves against the destination. Call before any migration write.
    """
    tier = await tier_for_tenant(target_tenant_id)
    if tier is None:
        return
    plan = plan_for(tier)
    async with connection_ctx() as db:
        for agent_id in sorted(agent_ids):
            _ = await dao.lock_usage(db, agent_id)
        for agent_id in agent_ids:
            usage = await dao.usage(db, agent_id)
            if usage.charged_bytes > plan.storage_limit_bytes or usage.largest_object_bytes > plan.max_file_size_bytes:
                raise StorageTransferBlockedError(
                    int(plan.storage_limit_bytes),
                    int(plan.max_file_size_bytes),
                    usage.charged_bytes,
                    usage.largest_object_bytes,
                )
