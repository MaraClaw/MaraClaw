"""Admin-only discovery and atomic company MCP catalog imports."""

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError

from app.core.outbound_policy import OutboundPolicyError, validate_public_url
from app.db.session import connection_ctx
from app.schemas.operator_response_tools import MCPCatalogImportResponse, MCPTestSuccess
from app.schemas.tool_management import MCPCatalogDiscovery, MCPCatalogImport, MCPDiscoveredTool
from app.services.mcp_client import MCPClient, MCPClientError
from app.services.mcp_installation import persist_admin_mcp_catalog
from app.services.tool_management_auth import ToolActor, catalog_tenant, fresh_tool_actor, get_tool_actor

router = APIRouter()


async def discover_catalog(data: MCPCatalogDiscovery) -> tuple[MCPClient, list[MCPDiscoveredTool]]:
    try:
        url = await validate_public_url(data.server_url)
        client = MCPClient(url, api_key=data.api_key)
        inventory = [MCPDiscoveredTool.model_validate(tool) for tool in await client.list_tools()]
        return client, inventory
    except OutboundPolicyError:
        raise HTTPException(400, "MCP server URL is not allowed") from None
    except MCPClientError, ValidationError, json.JSONDecodeError, TypeError:
        raise HTTPException(502, "MCP discovery failed") from None


@router.post("/mcp/discover")
async def discover_mcp_catalog(data: MCPCatalogDiscovery, actor: ToolActor = Depends(get_tool_actor)) -> MCPTestSuccess:
    tenant_id = catalog_tenant(actor.user, data.tenant_id)
    _, inventory = await discover_catalog(data)
    async with connection_ctx():
        fresh = await fresh_tool_actor(actor.user, tenant_id, actor.generation)
        _ = catalog_tenant(fresh, tenant_id)
    return {
        "ok": True,
        "tools": [
            {"name": tool.name, "description": tool.description, "inputSchema": tool.input_schema} for tool in inventory
        ],
    }


@router.post("/mcp/import")
async def import_mcp_catalog(
    data: MCPCatalogImport, actor: ToolActor = Depends(get_tool_actor)
) -> MCPCatalogImportResponse:
    _ = catalog_tenant(actor.user, data.tenant_id)
    client, inventory = await discover_catalog(data)
    sanitized = data.model_copy(update={"server_url": client.server_url, "api_key": client.api_key})
    imported = await persist_admin_mcp_catalog(actor, sanitized, inventory)
    return {"ok": True, "tools": imported}
