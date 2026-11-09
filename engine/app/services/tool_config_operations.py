"""Atomic stored-layer edits and authorized, uncached probe configuration."""

from dataclasses import dataclass

from fastapi import HTTPException

from app.core.json_types import JsonObject, json_object_from
from app.core.permissions import check_agent_access
from app.dao.tool_config_dao import lock_agent_config, lock_tool_config, patch_config_layer
from app.dao.tool_dao import agent_tool_dao, tool_dao
from app.db.session import connection_ctx
from app.records.tool import ToolRecord
from app.records.user import UserRecord
from app.schemas.tool_management import ConfigOperations, SavedToolReference
from app.services.tool_config import (
    apply_config_operations,
    decrypt_sensitive_fields,
    encrypted_config_operations,
    get_tool_company_config,
)
from app.services.tool_management_auth import ToolActor, catalog_tenant, fresh_tool_actor
from app.services.tool_ownership import can_configure_tool

NETWORK_PROXY_FIELDS = frozenset({"allow_network", "http_proxy", "https_proxy", "no_proxy"})


def authorize_config_operations(user: UserRecord, operations: ConfigOperations) -> None:
    if (operations.set.keys() | set(operations.unset)) & NETWORK_PROXY_FIELDS and (
        not user.is_platform_admin and user.role != "org_admin"
    ):
        raise HTTPException(403, "Only platform admin or organization admin can modify network or proxy settings")


async def scoped_tool(reference: SavedToolReference, actor: UserRecord) -> ToolRecord:
    tool = await tool_dao.get(reference.tool_id)
    if tool is None:
        raise HTTPException(404, "Tool not found")
    if reference.agent_id is None:
        tenant_id = catalog_tenant(actor, reference.tenant_id)
        if not can_configure_tool(actor, tool, tenant_id):
            raise HTTPException(403, "Cannot configure shared or another tenant's tools")
    else:
        agent, access = await check_agent_access(actor, reference.agent_id, fresh=True)
        if access != "manage":
            raise HTTPException(403, "Manage access required")
        assignment = await agent_tool_dao.get_assignment(agent.id, tool.id)
        visible = (
            tool.source == "builtin"
            or (tool.source == "admin" and (tool.tenant_id is None or tool.tenant_id == agent.tenant_id))
            or assignment is not None
        )
        if not visible:
            raise HTTPException(404, "Tool not found")
    return tool


async def patch_stored_config(reference: SavedToolReference, operations: ConfigOperations, actor: ToolActor) -> None:
    async with connection_ctx():
        target = reference.tenant_id if reference.tenant_id is not None else actor.user.tenant_id
        fresh = await fresh_tool_actor(actor.user, target, actor.generation)
        if reference.agent_id is not None:
            agent, _ = await check_agent_access(fresh, reference.agent_id, fresh=True)
            await lock_agent_config(agent, reference.tool_id)
        await lock_tool_config(reference.tool_id)
        tool = await scoped_tool(reference, fresh)
        authorize_config_operations(fresh, operations)
        encrypted = encrypted_config_operations(operations, tool)
        stored_reference = (
            reference.model_copy(update={"tenant_id": target}) if reference.agent_id is None else reference
        )
        await patch_config_layer(tool, stored_reference, encrypted)


@dataclass(frozen=True, slots=True)
class SavedProbeConfig:
    tool: ToolRecord
    config: JsonObject


async def saved_probe_config(
    reference: SavedToolReference, operations: ConfigOperations, actor: ToolActor
) -> SavedProbeConfig:
    async with connection_ctx():
        target = reference.tenant_id if reference.tenant_id is not None else actor.user.tenant_id
        fresh = await fresh_tool_actor(actor.user, target, actor.generation)
        tool = await scoped_tool(reference, fresh)
        authorize_config_operations(fresh, operations)
        encrypted = encrypted_config_operations(operations, tool)
        if reference.agent_id is not None:
            agent, _ = await check_agent_access(fresh, reference.agent_id, fresh=True)
            assignment = await agent_tool_dao.get_assignment(agent.id, tool.id)
            inherited = await get_tool_company_config(tool, agent.tenant_id)
            if tool.source == "builtin":
                inherited = {**decrypt_sensitive_fields(json_object_from(tool.config), tool.config_schema), **inherited}
            layer = apply_config_operations(json_object_from(assignment.config if assignment else {}), encrypted)
            config = {**inherited, **decrypt_sensitive_fields(layer, tool.config_schema)}
        else:
            layer = apply_config_operations(await get_tool_company_config(tool, target), operations)
            config = (
                {**decrypt_sensitive_fields(json_object_from(tool.config), tool.config_schema), **layer}
                if tool.source == "builtin"
                else layer
            )
        return SavedProbeConfig(tool, config)


async def reauthorize_saved_probe(reference: SavedToolReference, actor: ToolActor) -> None:
    async with connection_ctx():
        target = reference.tenant_id if reference.tenant_id is not None else actor.user.tenant_id
        fresh = await fresh_tool_actor(actor.user, target, actor.generation)
        _ = await scoped_tool(reference, fresh)
