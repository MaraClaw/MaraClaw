from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import HTTPException
from pydantic import TypeAdapter, ValidationError

from app.core.acl_locks import lock_agent
from app.core.domain.agents import AgentStatus, PermissionAccessLevel
from app.core.json_types import JsonObject, is_json_object
from app.core.permissions import check_agent_access
from app.core.security import load_user_principal
from app.dao.agent_mcp_dao import agent_mcp_server_dao
from app.db.session import connection_ctx, flush_request_transaction
from app.records.agent import AgentRecord
from app.schemas.agent_mcp import MCPServerCreate, MCPServerInventory, MCPServerPatch, MCPServerSummary
from app.services import agent_mcp_config as native
from app.services.agent_manager import agent_manager
from app.services.agent_mcp_sync import desired_servers, encrypted_record, sync_mcp_config
from app.services.openclaw_update_exclusion import locked_fresh_agent

_JSON = TypeAdapter(JsonObject)


async def _manager(user_id: UUID, agent_id: UUID) -> AgentRecord:
    actor = await load_user_principal(user_id, fresh=True)
    agent, level = await check_agent_access(actor, agent_id, fresh=True)
    if level != PermissionAccessLevel.MANAGE:
        raise HTTPException(403, detail="mcp_management_requires_manage")
    return agent


@asynccontextmanager
async def _managed_config(agent_id: UUID, user_id: UUID) -> AsyncGenerator[tuple[AgentRecord, dict[str, JsonObject]]]:
    previous = await _manager(user_id, agent_id)
    async with locked_fresh_agent(agent_id), connection_ctx() as db:
        await lock_agent(db, agent_id, previous.tenant_id)
        agent = await _manager(user_id, agent_id)
        desired = await desired_servers(agent_id)
        yield agent, {name: entry for name, entry in desired.items() if entry is not None}


async def list_servers(agent_id: UUID, user_id: UUID) -> MCPServerInventory:
    async with _managed_config(agent_id, user_id) as (_, servers):
        return MCPServerInventory(
            servers=[native.server_summary(name, server) for name, server in sorted(servers.items())]
        )


async def _publish(agent: AgentRecord, name: str, entry: JsonObject | None) -> bool:
    await agent_mcp_server_dao.save(encrypted_record(agent.id, name, entry))
    # The router deliberately has no request-wide CRUD binding. Desired state commits before filesystem I/O.
    await flush_request_transaction()
    try:
        return await sync_mcp_config(
            agent.id, agent_manager._agent_dir(agent.id) / "openclaw.json", running=agent.status == AgentStatus.RUNNING
        )
    except HTTPException as exc:
        raise HTTPException(
            503, detail={"code": "mcp_saved_sync_pending", "saved": True, "sync_error": exc.detail}
        ) from None


async def add_server(agent_id: UUID, user_id: UUID, payload: MCPServerCreate) -> tuple[MCPServerSummary, bool]:
    async with _managed_config(agent_id, user_id) as (agent, servers):
        if payload.name in servers:
            raise HTTPException(409, detail="mcp_server_exists")
        fields = payload.model_dump(exclude={"name"}, exclude_none=True, exclude_unset=True)
        fields["enabled"] = payload.enabled
        entry = native.native_fields(_JSON.validate_python(fields))
        applied = await _publish(agent, payload.name, entry)
        return native.server_summary(payload.name, entry), applied


async def configure_server(
    agent_id: UUID, user_id: UUID, name: str, payload: MCPServerPatch
) -> tuple[MCPServerSummary, bool]:
    async with _managed_config(agent_id, user_id) as (agent, servers):
        if name not in servers:
            raise HTTPException(404, detail="mcp_server_not_found")
        try:
            native.parse_server(servers[name]).check_transport_fields(payload.model_fields_set)
        except ValueError:
            raise HTTPException(422, detail="mcp_transport_fields_invalid") from None
        entry = dict(servers[name])
        changes = native.native_fields(
            _JSON.validate_python(payload.model_dump(exclude_unset=True, exclude={"removed_headers", "removed_env"}))
        )
        for key in ("headers", "env"):
            existing = entry.get(key, {})
            supplied = changes.pop(key, {})
            if not is_json_object(existing) or not is_json_object(supplied):
                raise HTTPException(409, detail="mcp_config_invalid")
            removed = payload.removed_headers if key == "headers" else payload.removed_env
            normalized = {item.lower() if key == "headers" else item for item in [*removed, *supplied]}
            merged = {
                item: value
                for item, value in existing.items()
                if (item.lower() if key == "headers" else item) not in normalized
            }
            if key in entry or key in payload.model_fields_set or removed:
                entry[key] = {**merged, **supplied}
        for key, value in changes.items():
            if value is None:
                entry.pop(key, None)
            else:
                entry[key] = value
        try:
            summary = native.server_summary(name, entry)
        except ValidationError:
            raise HTTPException(422, detail="mcp_transport_fields_invalid") from None
        applied = await _publish(agent, name, entry)
        return summary, applied


async def delete_server(agent_id: UUID, user_id: UUID, name: str) -> bool:
    async with _managed_config(agent_id, user_id) as (agent, servers):
        if name not in servers:
            raise HTTPException(404, detail="mcp_server_not_found")
        return await _publish(agent, name, None)
