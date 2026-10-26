from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException
from pydantic import TypeAdapter

from app.core.domain.agents import PermissionAccessLevel
from app.core.permissions import compute_agent_access_level
from app.core.row_memo import clear_row_memo
from app.core.security import load_user_principal
from app.dao.agent_dao import agent_dao
from app.dao.agent_lifecycle_projection_dao import agent_lifecycle_projection_dao
from app.dao.agent_setup_dao import agent_setup_dao
from app.dao.agent_setup_mcp_dao import agent_setup_mcp_dao
from app.db.session import connection_ctx
from app.schemas.agent_lifecycle import (
    AgentLifecycleAvailabilityOut,
    AgentLifecycleOut,
    AgentLifecycleRuntimeOut,
    LifecycleBlockCode,
)
from app.services.agent_work_eligibility import agent_work_blocking_codes
from app.services.openclaw_update_service import agent_update


async def get_agent_lifecycle(agent_id: UUID, user_id: UUID) -> AgentLifecycleOut:
    async with connection_ctx() as db:
        clear_row_memo()
        actor = await load_user_principal(user_id, fresh=True)
        agent = await agent_dao.get(agent_id)
        if agent is None:
            raise HTTPException(404, "Agent not found")
        if actor.tenant_id is not None and not await db.fetchval(
            "SELECT is_active FROM tenants WHERE id=%(id)s", {"id": actor.tenant_id}
        ):
            raise HTTPException(403, {"code": "tenant_inactive"})
        if await compute_agent_access_level(actor, agent) != PermissionAccessLevel.MANAGE:
            raise HTTPException(403, {"code": "agent_lifecycle_manage_required"})
        setup = await agent_setup_dao.setup_status(agent_id)
        if setup is None:
            raise HTTPException(404, "Agent not found")
        start = await agent_lifecycle_projection_dao.start(db, agent_id)
        optional_mcp = tuple(await agent_setup_mcp_dao.statuses(agent_id))
        upgrade = await agent_update(agent, PermissionAccessLevel.MANAGE)
        policy_codes = await agent_work_blocking_codes(agent)
        blockers: list[LifecycleBlockCode] = list(policy_codes)
        if setup.setup_pending:
            blockers.append("setup_pending")
        if agent.status == "stopped":
            blockers.append("runtime_stopped")
        elif agent.status != "running":
            blockers.append("runtime_not_running")
        upgrade_active = upgrade.run is not None and upgrade.run.state in ("pending", "cutover")
        if upgrade_active:
            blockers.append("upgrade_in_progress")
        if upgrade.required:
            blockers.append("upgrade_required")
        policy_eligible = not policy_codes
        return AgentLifecycleOut(
            as_of=TypeAdapter(datetime).validate_python(await db.fetchval("SELECT clock_timestamp()")).astimezone(UTC),
            runtime=AgentLifecycleRuntimeOut(recorded_status=agent.status, recorded_container_id=agent.container_id),
            setup=setup,
            start=start,
            optional_mcp=optional_mcp,
            upgrade=upgrade,
            availability=AgentLifecycleAvailabilityOut(
                policy_eligible=policy_eligible,
                blocking_codes=tuple(blockers),
                can_start=policy_eligible and not setup.setup_pending and not upgrade_active,
                can_retry_setup=policy_eligible and setup.retryable and not upgrade_active,
                can_retry_mcp=policy_eligible and any(item.retryable for item in optional_mcp) and not upgrade_active,
            ),
        )
