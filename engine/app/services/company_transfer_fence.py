from collections.abc import Sequence
from uuid import UUID

from anyio import current_time
from fastapi import HTTPException

from app.dao.agent_dao import agent_dao
from app.records.agent import AgentRecord
from app.records.user import UserRecord
from app.services.agent_manager import agent_manager
from app.services.agent_setup_lock import WorkspaceLockTimeoutError, setup_workspace_lock
from app.services.openclaw_update_gate import assert_no_active_run


async def fence_owned_agents(user: UserRecord) -> frozenset[UUID]:
    return await fence_agents(await agent_dao.list_for_creator(user.id, tenant_id=user.tenant_id))


async def fence_agents(agents: Sequence[AgentRecord]) -> frozenset[UUID]:
    deadline = current_time() + 5
    for agent in sorted(agents, key=lambda item: item.id):
        await assert_no_active_run(agent.id)
        try:
            async with setup_workspace_lock(
                agent_manager._agent_dir(agent.id),
                until_transaction_end=True,
                timeout_seconds=max(0, deadline - current_time()),
            ):
                await assert_no_active_run(agent.id)
        except WorkspaceLockTimeoutError as exc:
            raise HTTPException(409, {"code": "transfer_workspace_busy", "retryable": True}) from exc
    return frozenset(agent.id for agent in agents)
