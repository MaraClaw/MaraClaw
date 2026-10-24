import sqlite3
from typing import assert_never

from anyio import to_thread
from fastapi import HTTPException

from app.core.json_types import str_from_row
from app.core.logging import logger
from app.core.row_memo import memo_drop
from app.dao.agent_dao import agent_dao
from app.dao.company_transfer_cleanup_dao import company_transfer_cleanup_dao as cleanup
from app.dao.company_transfer_dao import company_transfer_dao
from app.dao.llm_dao import llm_model_dao
from app.db.session import connection_ctx
from app.records.company_transfer import TransferChoice
from app.records.company_transfer_cleanup import TransferCleanupRecord
from app.services.agent_manager import _guest_config_fingerprints, agent_manager
from app.services.agent_setup_lock import setup_workspace_lock
from app.services.company_transfer_credentials import scrub_credentials
from app.services.openclaw_update_gate import OpenClawUpdateGateError, assert_no_active_run


async def complete_cleanup(item: TransferCleanupRecord) -> str | None:
    async with setup_workspace_lock(agent_manager._agent_dir(item.agent_id)):
        if not await cleanup.is_pending(item.agent_id):
            return None
        await assert_no_active_run(item.agent_id)
        memo_drop("agent", item.agent_id)
        agent = await agent_dao.get(item.agent_id)
        if agent is None:
            return "agent_missing"
        if agent.container_id not in (None, "") and agent_manager.docker_client is None:
            return "docker_unavailable"
        if not await agent_manager.remove_container(agent):
            return "container_removal_failed"
        transfer = await company_transfer_dao.get(item.transfer_request_id)
        if transfer is None:
            return "transfer_missing"
        async with connection_ctx() as db:
            names = await db.fetchall(
                "SELECT mcp_server_name FROM tools WHERE tenant_id=%(source)s AND mcp_server_name IS NOT NULL",
                {"source": transfer.source_tenant_id},
            )
        source_names = frozenset(str_from_row(row["mcp_server_name"]) for row in names)
        match item.choice:
            case TransferChoice.FREE:
                await to_thread.run_sync(
                    lambda: scrub_credentials(
                        agent_manager._agent_dir(agent.id), free=True, source_tool_names=source_names
                    )
                )
            case TransferChoice.PAID:
                models = await llm_model_dao.list_enabled(tenant_id=transfer.target_tenant_id)
                primary = next((model for model in models if model.id == agent.primary_model_id), None)
                primary = primary or next(iter(models), None)
                secondary = next((model for model in models if model.id == agent.secondary_model_id), None)
                fallback = next((model for model in models if model.id == agent.fallback_model_id), None)
                await to_thread.run_sync(
                    lambda: scrub_credentials(
                        agent_manager._agent_dir(agent.id), free=False, source_tool_names=source_names
                    )
                )
                _guest_config_fingerprints.pop(str(agent.id), None)
                _ = await to_thread.run_sync(
                    lambda: agent_manager.write_guest_config(
                        agent, primary=primary, secondary=secondary, fallback=fallback
                    )
                )
            case unreachable:
                assert_never(unreachable)
        _ = await agent_dao.update(db_obj=agent, obj_in={"container_id": None, "container_port": None})
        await cleanup.record_attempt(item.agent_id, None)
    return None


async def reconcile_transfer_cleanup() -> None:
    for item in await cleanup.pending():
        try:
            error = await complete_cleanup(item)
        except (OSError, sqlite3.Error, ValueError, HTTPException, OpenClawUpdateGateError) as exc:
            error = type(exc).__name__
        if error is not None:
            logger.warning("Transfer runtime cleanup pending agent_id={} error={}", item.agent_id, error)
            await cleanup.record_attempt(item.agent_id, error)
