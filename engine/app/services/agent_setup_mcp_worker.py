import asyncio

import anyio
from fastapi import HTTPException

from app.core.logging import logger
from app.dao.agent_setup_mcp_dao import agent_setup_mcp_dao
from app.records.agent_setup_mcp import AgentSetupMCPWork
from app.services.agent_manager import agent_manager
from app.services.agent_setup_lock import drain_on_cancel, setup_workspace_lock
from app.services.agent_setup_mcp import active_mcp_claim
from app.services.openclaw_update_gate import OpenClawUpdateInProgressError
from app.services.smithery_import import MCPImportOutcome, prepare_smithery_import, publish_smithery_import


class MCPClaimLostError(Exception):
    pass


async def _renew(claim: AgentSetupMCPWork) -> None:
    while True:
        if not await agent_setup_mcp_dao.renew(claim):
            return
        await anyio.sleep(60)


async def _execute(claim: AgentSetupMCPWork) -> None:
    async with setup_workspace_lock(agent_manager._agent_dir(claim.agent_id)):
        async with active_mcp_claim(claim) as active:
            if not active:
                return
        try:
            outcome = await prepare_smithery_import(claim.server_id, claim.agent_id)
        except Exception:
            logger.warning("Optional MCP import failed agent_id={} server_id={}", claim.agent_id, claim.server_id)
            outcome = MCPImportOutcome("import_failed", "")
        async with active_mcp_claim(claim) as active:
            if not active:
                return
            await publish_smithery_import(claim.agent_id, outcome)
            if not await agent_setup_mcp_dao.finish(claim, outcome.warning_code):
                raise MCPClaimLostError


async def _with_renewal(claim: AgentSetupMCPWork) -> None:
    renewal = asyncio.create_task(_renew(claim))
    try:
        await _execute(claim)
    finally:
        renewal.cancel()
        await asyncio.gather(renewal, return_exceptions=True)
        if not renewal.cancelled():
            renewal.result()


async def run_agent_setup_mcp(claim: AgentSetupMCPWork) -> None:
    try:
        await drain_on_cancel(_with_renewal(claim))
    except asyncio.CancelledError:
        await drain_on_cancel(agent_setup_mcp_dao.release(claim))
        raise
    except HTTPException as error:
        if error.status_code not in (401, 403, 404, 409, 423):
            raise
        await agent_setup_mcp_dao.release(claim)
    except OpenClawUpdateInProgressError:
        await agent_setup_mcp_dao.release(claim)
    except MCPClaimLostError:
        return
