from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import HTTPException

from app.core.acl_locks import lock_agent, lock_tenants, lock_user
from app.dao.agent_setup_mcp_dao import agent_setup_mcp_dao
from app.db.connection import DbConnection
from app.db.session import connection_ctx, flush_request_transaction
from app.records.agent_setup_mcp import AgentSetupMCPWork
from app.records.user import UserRecord
from app.schemas.agent_setup_mcp import AgentSetupMCPStatus
from app.services.agent_manager import agent_manager
from app.services.agent_setup_lock import setup_workspace_lock
from app.services.openclaw_update_gate import assert_no_active_run


async def _lock_subjects(db: DbConnection, work: AgentSetupMCPWork, actor: UserRecord | None) -> None:
    await lock_tenants(db, (work.tenant_id, actor.tenant_id if actor else None))
    subjects = {work.owner_id: work.tenant_id}
    if actor is not None:
        subjects[actor.id] = actor.tenant_id
    for user_id in sorted(subjects):
        await lock_user(db, user_id, subjects[user_id])
    await db.fetchall(
        """SELECT i.id FROM identities i JOIN users u ON u.identity_id=i.id
        WHERE u.id=ANY(%(ids)s::uuid[]) ORDER BY i.id FOR UPDATE OF i""",
        {"ids": list(subjects)},
    )
    await lock_agent(db, work.agent_id, work.tenant_id)
    await db.fetchone(
        "SELECT agent_id FROM agent_setup_mcp_work WHERE agent_id=%(agent)s AND server_id=%(server)s FOR UPDATE",
        {"agent": work.agent_id, "server": work.server_id},
    )


@asynccontextmanager
async def active_mcp_claim(work: AgentSetupMCPWork) -> AsyncGenerator[bool]:
    async with connection_ctx() as db:
        await _lock_subjects(db, work, None)
        await assert_no_active_run(work.agent_id)
        yield await agent_setup_mcp_dao.owns(work) and await agent_setup_mcp_dao.eligible(work)


async def retry_agent_setup_mcp(agent_id: UUID, user_id: UUID, server_id: str) -> AgentSetupMCPStatus:
    from app.services.agent_setup import _active_setup_tenants, _setup_manager

    _ = await _setup_manager(user_id, agent_id)
    async with setup_workspace_lock(agent_manager._agent_dir(agent_id)), connection_ctx() as db:
        actor, agent = await _setup_manager(user_id, agent_id)
        work = await agent_setup_mcp_dao.get(agent_id, server_id)
        if work is None:
            raise HTTPException(404, detail={"code": "agent_setup_mcp_unknown_server"})
        try:
            await _lock_subjects(db, work, actor)
        except HTTPException as error:
            if error.status_code != 409:
                raise
            raise HTTPException(409, detail={"code": "agent_setup_mcp_ineligible"}) from error
        current, agent = await _setup_manager(user_id, agent_id)
        if current.identity_id != actor.identity_id:
            raise HTTPException(409, detail={"code": "principal_changed"})
        await _active_setup_tenants(db, current, agent)
        await assert_no_active_run(agent_id)
        work = await agent_setup_mcp_dao.get(agent_id, server_id)
        if work is None:
            raise HTTPException(404, detail={"code": "agent_setup_mcp_unknown_server"})
        if not await agent_setup_mcp_dao.eligible(work):
            raise HTTPException(409, detail={"code": "agent_setup_mcp_ineligible"})
        match work.state:
            case "pending" | "running":
                pass
            case "warning" if work.warning_code != "unsupported_remote_transport":
                await agent_setup_mcp_dao.retry(work, current.id)
            case "warning" | "succeeded":
                raise HTTPException(409, detail={"code": "agent_setup_mcp_not_retryable"})
        result = next(item for item in await agent_setup_mcp_dao.statuses(agent_id) if item.server_id == server_id)
        await flush_request_transaction()
        return result
