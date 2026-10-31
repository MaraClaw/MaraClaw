from uuid import UUID

from fastapi import HTTPException

from app.core.acl_locks import lock_agent, lock_tenants, lock_user
from app.core.row_memo import clear_entity_memo
from app.core.security import load_user_principal
from app.dao.agent_dao import agent_dao
from app.dao.company_admission_dao import company_admission_dao
from app.db.session import connection_ctx
from app.records.agent import AgentRecord
from app.records.user import UserRecord


async def personal_owner(agent_id: UUID, actor_id: UUID | None = None) -> tuple[AgentRecord, UserRecord]:
    clear_entity_memo()
    agent = await agent_dao.get(agent_id)
    if agent is None or agent.tenant_id is None:
        raise HTTPException(404, "Agent unavailable")
    async with connection_ctx() as db:
        await lock_tenants(db, (agent.tenant_id,))
        await lock_user(db, agent.creator_id, agent.tenant_id)
        await lock_agent(db, agent.id, agent.tenant_id)
        clear_entity_memo()
        current = await agent_dao.get(agent_id)
        if current is None or current.creator_id != agent.creator_id or current.tenant_id != agent.tenant_id:
            raise HTTPException(409, "Agent ownership changed")
        if actor_id is not None and actor_id != current.creator_id:
            raise HTTPException(403, "Only creator can configure personal WhatsApp")
        owner = await load_user_principal(current.creator_id, fresh=True)
        tenant = await company_admission_dao.tenant(agent.tenant_id)
        if owner.tenant_id != current.tenant_id or tenant is None or not tenant.is_active:
            raise HTTPException(404, "Agent unavailable")
        return current, owner
