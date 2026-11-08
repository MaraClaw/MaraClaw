"""Atomic persistence of DB-owned MCP installations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from app.core.json_types import JsonObject
from app.dao.tool_dao import agent_tool_dao, tool_dao
from app.db.errors import UniqueViolationError
from app.db.session import connection_ctx
from app.schemas.operator_response_tools import ToolCreatedResponse
from app.schemas.tool_management import MCPCatalogImport, MCPDiscoveredTool
from app.services.tool_config import encrypt_sensitive_fields
from app.services.tool_management_auth import ToolActor, catalog_tenant, fresh_tool_actor

if TYPE_CHECKING:
    from app.services.resource_discovery import DiscoveredMCPTool


@dataclass(frozen=True, slots=True)
class MCPInstallation:
    prefix: str
    display_name: str
    server_url: str
    description: str
    config: JsonObject


async def persist_mcp_installation(
    agent_id: UUID, installation: MCPInstallation, tools: list[DiscoveredMCPTool]
) -> list[str]:
    config = encrypt_sensitive_fields(
        installation.config, {"fields": [{"key": "atlassian_api_key", "type": "password"}]}
    )
    imported: list[str] = []
    async with connection_ctx() as db, db.raw.transaction():
        for discovered in tools or [None]:
            remote_name = discovered["name"] if discovered else None
            name = f"{installation.prefix}_{remote_name}" if remote_name is not None else installation.prefix
            display = (
                f"{installation.display_name}: {remote_name}" if remote_name is not None else installation.display_name
            )
            existing = await tool_dao.get_by_name(name)
            if existing is not None:
                updates: JsonObject = {"mcp_server_url": installation.server_url}
                if remote_name is None:
                    updates["enabled"] = False
                tool = await tool_dao.update(db_obj=existing, obj_in=updates)
            else:
                tool = await tool_dao.create(
                    obj_in={
                        "name": name,
                        "display_name": display,
                        "description": discovered["description"] if discovered else installation.description[:500],
                        "type": "mcp",
                        "category": "mcp",
                        "icon": "🔌",
                        "parameters_schema": discovered["parameters_schema"]
                        if discovered
                        else {"type": "object", "properties": {}},
                        "mcp_server_url": installation.server_url,
                        "mcp_server_name": installation.display_name,
                        "mcp_tool_name": remote_name,
                        "enabled": remote_name is not None,
                        "is_default": False,
                        "source": "agent",
                    }
                )
            _ = await agent_tool_dao.ensure_with_config(
                agent_id, tool.id, config=config, source="user_installed", installed_by_agent_id=agent_id
            )
            imported.append(f"{'⏭️' if existing else '✅'} {display}")

        if tools:
            generic = await tool_dao.get_by_name(installation.prefix)
            if generic is not None:
                assignment = await agent_tool_dao.get_assignment(agent_id, generic.id)
                if assignment is not None:
                    _ = await agent_tool_dao.delete(record_id=assignment.id)
    return imported


async def persist_admin_mcp_catalog(
    actor: ToolActor, request: MCPCatalogImport, inventory: list[MCPDiscoveredTool]
) -> list[ToolCreatedResponse]:
    """Insert a rediscovered selection atomically without touching agent installations."""
    from fastapi import HTTPException

    current = {tool.name: tool for tool in inventory}
    if any(current.get(tool.name) != tool for tool in request.tools):
        raise HTTPException(409, "Selected MCP definitions changed; discover again")
    tenant_id = catalog_tenant(actor.user, request.tenant_id)
    names = [f"{request.name_prefix}_{tool.name}" for tool in request.tools]
    config = encrypt_sensitive_fields({"api_key": request.api_key} if request.api_key not in (None, "") else {})
    imported: list[ToolCreatedResponse] = []
    try:
        async with connection_ctx() as db, db.raw.transaction():
            fresh = await fresh_tool_actor(actor.user, tenant_id, actor.generation)
            if catalog_tenant(fresh, request.tenant_id) != tenant_id:
                raise HTTPException(403, "Company authority changed")
            if await tool_dao.list_by_names(names):
                raise HTTPException(409, "One or more local tool names are unavailable")
            for name, remote in zip(names, request.tools, strict=True):
                tool = await tool_dao.create(
                    obj_in={
                        "name": name,
                        "display_name": f"{request.server_name}: {remote.name}",
                        "description": remote.description,
                        "type": "mcp",
                        "category": "mcp",
                        "icon": "🔌",
                        "parameters_schema": remote.input_schema,
                        "mcp_server_url": request.server_url,
                        "mcp_server_name": request.server_name,
                        "mcp_tool_name": remote.name,
                        "enabled": True,
                        "is_default": False,
                        "source": "admin",
                        "tenant_id": tenant_id,
                        "config": config,
                    }
                )
                imported.append({"id": str(tool.id), "name": tool.name})
    except UniqueViolationError:
        raise HTTPException(409, "One or more local tool names are unavailable") from None
    return imported
