from uuid import UUID

from app.db.connection import DbConnection


async def retains_agent(db: DbConnection, agent_id: UUID) -> bool:
    return bool(
        await db.fetchval(
            "SELECT EXISTS(SELECT 1 FROM company_transfer_retained_agents WHERE agent_id=%(id)s)",
            {"id": agent_id},
        )
    )


async def retains_tenant(db: DbConnection, tenant_id: UUID) -> bool:
    return bool(
        await db.fetchval(
            "SELECT EXISTS(SELECT 1 FROM company_transfer_retained_agents r "
            "JOIN company_transfer_requests t ON t.id=r.transfer_request_id "
            "JOIN agents a ON a.id=r.agent_id "
            "JOIN users original ON original.id=r.original_user_id "
            "JOIN users holder ON holder.id=r.holder_user_id "
            "WHERE r.source_tenant_id=%(id)s OR t.source_tenant_id=%(id)s OR t.target_tenant_id=%(id)s "
            "OR a.tenant_id=%(id)s OR original.tenant_id=%(id)s OR holder.tenant_id=%(id)s)",
            {"id": tenant_id},
        )
    )
