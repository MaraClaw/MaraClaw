from uuid import UUID

from fastapi import HTTPException

from app.core.acl_locks import lock_agent, lock_tenants
from app.dao.company_transfer_retention_dao import retains_agent, retains_tenant
from app.db.connection import DbConnection
from app.records.agent import AgentRecord


async def assert_agent_deletion_allowed(db: DbConnection, agent: AgentRecord) -> None:
    await lock_tenants(db, (agent.tenant_id,))
    await lock_agent(db, agent.id, agent.tenant_id)
    if await retains_agent(db, agent.id):
        raise HTTPException(
            409,
            {"code": "transfer_retention_conflict", "message": "Retained company-transfer agents cannot be deleted"},
        )


async def assert_tenant_deletion_allowed(db: DbConnection, tenant_id: UUID) -> None:
    await lock_tenants(db, (tenant_id,))
    if await retains_tenant(db, tenant_id):
        raise HTTPException(
            409,
            {
                "code": "transfer_retention_conflict",
                "message": "Retained company-transfer records prevent company deletion",
            },
        )
