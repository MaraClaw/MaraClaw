"""Worker-owned replay of durable creation inputs, not request-owned background work."""

import asyncio
import base64
from uuid import UUID

import anyio
from fastapi import HTTPException
from psycopg import OperationalError
from psycopg_pool import PoolTimeout

from app.config import get_settings
from app.core.acl_locks import lock_agent, lock_tenants, lock_user
from app.core.logging import logger, new_trace_id
from app.core.permissions import check_agent_access
from app.core.row_memo import clear_row_memo
from app.core.security import decrypt_data, encrypt_data, load_user_principal
from app.dao import agent_dao, agent_setup_dao, agent_template_dao, skill_dao
from app.db.connection import DbConnection
from app.db.errors import DbError
from app.db.session import connection_ctx, flush_request_transaction
from app.records.agent import AgentRecord
from app.records.agent_setup import AgentSetupInputs, AgentSetupRecord, AgentSetupStatus
from app.records.user import UserRecord
from app.schemas.schemas import AgentCreate
from app.services.agent_manager import agent_manager
from app.services.agent_setup_lock import drain_on_cancel, setup_workspace_lock
from app.services.agent_workspace import render_agent_files, write_missing_agent_files
from app.services.gogcli_runtime import gogcli_skill_folder_names
from app.services.okr_agent_hook import hook_new_agent
from app.services.openclaw_keys import write_gateway_api_key
from app.services.openclaw_update_exclusion import locked_fresh_agent
from app.services.resource_discovery import import_mcp_from_smithery

_WORKER_IO_ERRORS = (DbError, OperationalError, PoolTimeout, OSError)


async def _setup_manager(user_id: UUID, agent_id: UUID) -> tuple[UserRecord, AgentRecord]:
    actor = await load_user_principal(user_id, fresh=True)
    agent, level = await check_agent_access(actor, agent_id, fresh=True)
    if level != "manage":
        raise HTTPException(403, detail={"code": "agent_setup_manage_required"})
    return actor, agent


async def _active_setup_tenants(db: DbConnection, actor: UserRecord, agent: AgentRecord) -> None:
    for tenant_id in {actor.tenant_id, agent.tenant_id} - {None}:
        row = await db.fetchone("SELECT is_active FROM tenants WHERE id = %(id)s", {"id": tenant_id})
        if row is None or not row["is_active"]:
            raise HTTPException(403, detail={"code": "tenant_inactive"})


async def get_agent_setup_status(agent_id: UUID, user_id: UUID) -> AgentSetupStatus:
    async with connection_ctx() as db:
        actor, agent = await _setup_manager(user_id, agent_id)
        await _active_setup_tenants(db, actor, agent)
        result = await agent_setup_dao.setup_status(agent_id)
        if result is None:
            raise HTTPException(404, "Agent not found")
        return result


async def retry_agent_setup(agent_id: UUID, user_id: UUID) -> AgentSetupStatus:
    _ = await _setup_manager(user_id, agent_id)
    async with locked_fresh_agent(agent_id) as snapshot, connection_ctx() as db:
        actor = await load_user_principal(user_id, fresh=True)
        await lock_tenants(db, (actor.tenant_id, snapshot.tenant_id))
        await lock_user(db, actor.id, actor.tenant_id)
        if actor.identity_id is not None:
            await db.fetchone("SELECT id FROM identities WHERE id = %(id)s FOR UPDATE", {"id": actor.identity_id})
        await lock_agent(db, agent_id, snapshot.tenant_id)
        current, agent = await _setup_manager(user_id, agent_id)
        if current.identity_id != actor.identity_id:
            raise HTTPException(409, detail={"code": "principal_changed"})
        await _active_setup_tenants(db, current, agent)
        result = await agent_setup_dao.retry_failed(agent_id)
        if result is None:
            raise HTTPException(409, detail={"code": "agent_setup_not_retryable"})
        await flush_request_transaction()
        return result


def _expected_worker_io(error: BaseException) -> bool:
    if isinstance(error, DbError):
        return isinstance(error.orig, (OperationalError, OSError))
    return isinstance(error, (OperationalError, PoolTimeout, OSError))


async def persist_agent_setup(agent: AgentRecord, data: AgentCreate, raw_key: str) -> None:
    files = await render_agent_files(
        agent, agent_manager._template_dir(), personality=data.personality or "", boundaries=data.boundaries or ""
    )
    folders: list[str] = []
    servers: tuple[str, ...] = ()
    if data.template_id:
        template = await agent_template_dao.get(data.template_id)
        if template:
            folders.extend(template.default_skills or [])
            servers = tuple(template.default_mcp_servers)
    if data.gogcli_enabled:
        folders.extend(gogcli_skill_folder_names())
    skill_ids = (
        set(data.skill_ids or [])
        | await skill_dao.list_default_ids()
        | await skill_dao.list_ids_by_folder_names(folders)
    )
    for skill in await skill_dao.list_with_files_by_ids(sorted(skill_ids)) if skill_ids else []:
        for file in skill.files:
            files[f"skills/{skill.folder_name}/{file.path}"] = file.content.encode()
    inputs = AgentSetupInputs(
        files={path: base64.b64encode(content).decode("ascii") for path, content in files.items()},
        gateway_key_encrypted=encrypt_data(raw_key, get_settings().SECRET_KEY),
        mcp_servers=servers,
    )
    _ = await agent_setup_dao.create(obj_in={"agent_id": agent.id, "inputs": inputs.model_dump(mode="json")})


async def _execute(claim: AgentSetupRecord) -> None:
    async with setup_workspace_lock(agent_manager._agent_dir(claim.agent_id)):
        if not await agent_setup_dao.owns(claim):
            return
        agent = await agent_dao.get(claim.agent_id)
        if agent is None:
            return
        write_gateway_api_key(agent, decrypt_data(claim.inputs.gateway_key_encrypted, get_settings().SECRET_KEY))
        await write_missing_agent_files(
            agent.id, {path: base64.b64decode(content) for path, content in claim.inputs.files.items()}
        )
        for server in claim.inputs.mcp_servers:
            if not await agent_setup_dao.owns(claim):
                return
            try:
                result = await import_mcp_from_smithery(server_id=server, agent_id=agent.id, config={})
                if result.startswith("❌"):
                    logger.warning("Agent setup optional MCP import failed agent_id={} server={}", agent.id, server)
            except Exception:
                logger.warning("Agent setup optional MCP import raised agent_id={} server={}", agent.id, server)
        if agent.tenant_id and await agent_setup_dao.owns(claim):
            await hook_new_agent(agent.id, agent.tenant_id)


async def _renew_lease(claim: AgentSetupRecord) -> None:
    try:
        while True:
            if not await agent_setup_dao.renew(claim):
                return
            await anyio.sleep(60)
    except _WORKER_IO_ERRORS as exc:
        if not _expected_worker_io(exc):
            raise
        logger.warning("Agent setup lease renewal stopped agent_id={}", claim.agent_id)


async def _execute_with_renewal(claim: AgentSetupRecord) -> None:
    renewal = asyncio.create_task(_renew_lease(claim))
    try:
        await _execute(claim)
    finally:
        _ = renewal.cancel()
        _ = await asyncio.gather(renewal, return_exceptions=True)
        if not renewal.cancelled():
            renewal.result()


async def run_agent_setup(claim: AgentSetupRecord) -> None:
    new_trace_id()
    clear_row_memo()
    failure_code = None
    try:
        await drain_on_cancel(_execute_with_renewal(claim))
    except asyncio.CancelledError:
        try:
            await drain_on_cancel(agent_setup_dao.release(claim))
        except _WORKER_IO_ERRORS as exc:
            if not _expected_worker_io(exc):
                raise
            logger.warning("Agent setup cancellation release deferred to lease expiry agent_id={}", claim.agent_id)
        raise
    except Exception:
        logger.exception("Agent setup failed agent_id={}", claim.agent_id)
        failure_code = "setup_failed"
    for attempt in range(3):
        try:
            _ = await agent_setup_dao.finish(claim, failure_code)
            return
        except _WORKER_IO_ERRORS as exc:
            if not _expected_worker_io(exc):
                raise
            logger.warning("Agent setup completion unavailable agent_id={} attempt={}", claim.agent_id, attempt + 1)
            if attempt < 2:
                await anyio.sleep(1)


async def start_agent_setup_worker() -> None:
    while True:
        new_trace_id()
        clear_row_memo()
        try:
            claim = await agent_setup_dao.claim()
        except _WORKER_IO_ERRORS as exc:
            if not _expected_worker_io(exc):
                raise
            logger.warning("Agent setup claim unavailable error_type={}", type(exc).__name__)
            await anyio.sleep(1)
            continue
        if claim is None:
            await anyio.sleep(1)
        else:
            await run_agent_setup(claim)
