from uuid import UUID

from app.dao.agent_dao import agent_dao
from app.records.user import UserRecord
from app.services.agent_manager import agent_manager
from app.services.agent_setup_lock import setup_workspace_lock
from app.services.openclaw_update_gate import assert_no_active_run


async def fence_owned_agents(user: UserRecord) -> frozenset[UUID]:
    agents = await agent_dao.list_for_creator(user.id, tenant_id=user.tenant_id)
    for agent in sorted(agents, key=lambda item: item.id):
        await assert_no_active_run(agent.id)
        async with setup_workspace_lock(agent_manager._agent_dir(agent.id), until_transaction_end=True):
            await assert_no_active_run(agent.id)
    return frozenset(agent.id for agent in agents)
