"""Tool management API - CRUD for tools and per-agent assignments."""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.json_types import JsonObject
from app.core.logging import logger
from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.dao.agent_dao import agent_dao
from app.dao.channel_config_dao import channel_config_dao
from app.dao.system_setting_dao import system_setting_dao
from app.dao.tool_dao import agent_tool_dao, tool_dao
from app.db.session import connection_ctx
from app.records.tool import AgentToolRecord, ToolRecord
from app.records.user import UserRecord
from app.services.email_service import EmailConfig
from app.services.tool_config import (
    decrypt_sensitive_fields,
    encrypt_sensitive_fields,
    get_sensitive_keys,
    get_tenant_tool_configs,
    get_tool_company_config,
    mask_sensitive_fields,
    meaningful_config,
    set_tenant_tool_config,
)

router = APIRouter(prefix="/tools", tags=["tools"])


CATEGORY_CONFIG_PRIMARY_TOOL = {
    "agentbay": "agentbay_browser_navigate",
}


async def _load_agent_for_tool_scope(agent_id: uuid.UUID):
    """Load the agent whose tenant boundary determines tool visibility."""
    agent = await agent_dao.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


async def _load_agent_tool_assignments(agent_id: uuid.UUID) -> dict[str, AgentToolRecord]:
    """Return explicit tool assignments for one agent keyed by tool ID string."""
    assignments = await agent_tool_dao.list_for_agent(agent_id)
    return {str(at.tool_id): at for at in assignments}


def _tool_record_visible_to_agent(
    tool: ToolRecord | Any,
    agent_tenant_id: uuid.UUID | None,
    assignments: dict[str, AgentToolRecord | Any],
) -> bool:
    """Pure visibility check for tools against an agent tenant boundary."""
    if str(tool.id) in assignments:
        return True
    if tool.source == "builtin":
        return True
    if tool.source == "admin":
        return tool.tenant_id is None or (agent_tenant_id is not None and tool.tenant_id == agent_tenant_id)
    if tool.source == "agent":
        return str(tool.id) in assignments
    return False


def _resolve_target_tenant_id(current_user: UserRecord, tenant_id: str | None = None) -> uuid.UUID | None:
    if tenant_id:
        try:
            resolved = uuid.UUID(tenant_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid tenant_id format") from exc
        if current_user.role != "platform_admin" and resolved != current_user.tenant_id:
            raise HTTPException(status_code=403, detail="Cannot manage another tenant's tools")
        return resolved
    return current_user.tenant_id


def _require_catalog_manager(current_user: UserRecord) -> None:
    if current_user.role not in {"platform_admin", "org_admin"}:
        raise HTTPException(status_code=403, detail="Admin access required")


def _get_sensitive_keys(config_schema: dict | None = None) -> set[str]:
    return get_sensitive_keys(config_schema)


def _encrypt_sensitive_fields(config: JsonObject, config_schema: dict | None = None) -> JsonObject:
    return encrypt_sensitive_fields(config, config_schema)


def _decrypt_sensitive_fields(config: JsonObject, config_schema: dict | None = None) -> JsonObject:
    return decrypt_sensitive_fields(config, config_schema)


# ─── Schemas ────────────────────────────────────────────────
class ToolCreate(BaseModel):
    name: str
    display_name: str
    description: str = ""
    type: str = "mcp"
    category: str = "custom"
    icon: str = "🔧"
    parameters_schema: dict = {"type": "object", "properties": {}}
    mcp_server_url: str | None = None
    mcp_server_name: str | None = None
    mcp_tool_name: str | None = None
    is_default: bool = False
    # Optional: platform admins can specify target tenant (e.g. when managing
    # another company's tools via the Enterprise Settings page).
    tenant_id: str | None = None


class ToolUpdate(BaseModel):
    display_name: str | None = None
    description: str | None = None
    icon: str | None = None
    enabled: bool | None = None
    mcp_server_url: str | None = None
    mcp_server_name: str | None = None
    parameters_schema: dict | None = None
    is_default: bool | None = None
    config: JsonObject | None = None
    tenant_id: str | None = None


class AgentToolUpdate(BaseModel):
    tool_id: str
    enabled: bool


class CategoryConfigUpdate(BaseModel):
    config: JsonObject


# ─── Global Tool CRUD ──────────────────────────────────────
@router.get("")
async def list_tools(tenant_id: str | None = None, current_user: UserRecord = Depends(get_current_user), db=None):
    """List platform tools scoped by tenant (builtin + tenant-specific)."""
    target_tenant_id = _resolve_target_tenant_id(current_user, tenant_id)
    tools = await tool_dao.list_platform_for_tenant(target_tenant_id)
    builtin_configs = await get_tenant_tool_configs(
        target_tenant_id,
        [t for t in tools if getattr(t, "source", None) == "builtin"],
    )
    response = []
    for t in tools:
        if getattr(t, "source", None) == "builtin":
            raw_config = builtin_configs.get(t.name, {})
        else:
            raw_config = await get_tool_company_config(db, t, target_tenant_id)
        company_config = mask_sensitive_fields(raw_config, t.config_schema)
        response.append(
            {
                "id": str(t.id),
                "name": t.name,
                "display_name": t.display_name,
                "description": t.description,
                "type": t.type,
                "category": t.category,
                "icon": t.icon,
                "parameters_schema": t.parameters_schema,
                "mcp_server_url": t.mcp_server_url,
                "mcp_server_name": t.mcp_server_name,
                "mcp_tool_name": t.mcp_tool_name,
                "enabled": t.enabled,
                "is_default": t.is_default,
                "source": t.source,
                "config": company_config,
                "config_schema": t.config_schema or {},
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
        )
    return response


@router.post("")
async def create_tool(data: ToolCreate, current_user: UserRecord = Depends(get_current_user)):
    """Create a new tool (typically MCP).

    The tool is scoped to the target tenant, which defaults to the caller's
    own tenant but can be overridden via data.tenant_id. This allows platform
    admins to import MCP tools while viewing another company's settings page.
    """
    _require_catalog_manager(current_user)
    target_tenant_id = _resolve_target_tenant_id(current_user, data.tenant_id)

    existing = await tool_dao.get_by_name_and_tenant(data.name, target_tenant_id)
    if existing:
        raise HTTPException(status_code=400, detail=f"Tool '{data.name}' already exists")

    tool = await tool_dao.create(
        obj_in={
            "name": data.name,
            "display_name": data.display_name,
            "description": data.description,
            "type": data.type,
            "category": data.category,
            "icon": data.icon,
            "parameters_schema": data.parameters_schema,
            "mcp_server_url": data.mcp_server_url,
            "mcp_server_name": data.mcp_server_name,
            "mcp_tool_name": data.mcp_tool_name,
            "is_default": data.is_default,
            "tenant_id": target_tenant_id,
            "source": "admin",
        }
    )
    return {"id": str(tool.id), "name": tool.name}


# NOTE: Literal path routes (/bulk, /mcp-server) MUST be defined BEFORE
# parameterized routes (/{tool_id}) to avoid older FastAPI/Starlette versions
# matching "bulk" as a uuid.UUID path parameter and returning 422.


class BulkToolUpdateItem(BaseModel):
    tool_id: str
    enabled: bool


@router.put("/bulk")
async def update_tools_bulk(updates: list[BulkToolUpdateItem], current_user: UserRecord = Depends(get_current_user)):
    """Bulk update the enabled status of multiple tools."""
    _require_catalog_manager(current_user)
    tool_ids = [uuid.UUID(u.tool_id) for u in updates]
    tools = await tool_dao.list_by_ids(tool_ids)
    tools_map = {str(t.id): t for t in tools}

    for update in updates:
        tool = tools_map.get(update.tool_id)
        if tool:
            await tool_dao.update(db_obj=tool, obj_in={"enabled": update.enabled})

    return {"ok": True}


@router.put("/{tool_id}")
async def update_tool(
    tool_id: uuid.UUID, data: ToolUpdate, current_user: UserRecord = Depends(get_current_user), db=None
):
    """Update a tool."""
    _require_catalog_manager(current_user)
    tool = await tool_dao.get(tool_id)
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")

    update_data = data.model_dump(exclude_unset=True)
    target_tenant_id = _resolve_target_tenant_id(
        current_user,
        data.tenant_id if "tenant_id" in update_data else None,
    )
    update_data.pop("tenant_id", None)

    if "config" in update_data:
        update_data.pop("config")
        config_value = meaningful_config(data.config)
        if tool.source == "builtin":
            if not target_tenant_id:
                raise HTTPException(status_code=400, detail="tenant_id is required to configure builtin tools")
            await set_tenant_tool_config(db, target_tenant_id, tool.name, config_value, tool.config_schema)
        else:
            update_data["config"] = _encrypt_sensitive_fields(config_value, tool.config_schema)

    if update_data:
        await tool_dao.update(db_obj=tool, obj_in=update_data)
    return {"ok": True}


@router.delete("/{tool_id}")
async def delete_tool(tool_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)):
    """Delete a tool (only non-builtin)."""
    _require_catalog_manager(current_user)
    tool = await tool_dao.get(tool_id)
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")
    if tool.type == "builtin":
        raise HTTPException(status_code=400, detail="Cannot delete builtin tools")
    if current_user.role != "platform_admin" and tool.tenant_id not in {None, current_user.tenant_id}:
        raise HTTPException(status_code=403, detail="Cannot delete another tenant's tools")

    async with connection_ctx():
        await agent_tool_dao.delete_for_tool(tool_id)
        await tool_dao.delete(id=tool_id)
    return {"ok": True}


# ─── Per-Agent Tool Assignment ─────────────────────────────
@router.get("/agents/{agent_id}")
async def get_agent_tools(agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)):
    """Get tools for a specific agent with their enabled status."""
    from app.services.agent_tools import _agent_has_feishu

    await check_agent_access(current_user, agent_id)
    has_feishu = await _agent_has_feishu(agent_id)

    agent_obj = await _load_agent_for_tool_scope(agent_id)
    is_system_agent = bool(agent_obj and agent_obj.is_system)

    assignments = await _load_agent_tool_assignments(agent_id)
    all_tools = await tool_dao.list_enabled_visible(
        agent_tenant_id=agent_obj.tenant_id,
        assigned_tool_ids=[uuid.UUID(tid) for tid in assignments],
    )

    if assignments:
        backfilled = 0
        for t in all_tools:
            tid = str(t.id)
            if tid not in assignments:
                new_at = await agent_tool_dao.create(
                    obj_in={
                        "agent_id": agent_id,
                        "tool_id": t.id,
                        "enabled": t.is_default,
                    }
                )
                assignments[tid] = new_at
                backfilled += 1
        if backfilled:
            logger.info(f"[Tools] Backfilled {backfilled} AgentTool records for agent={agent_id}")

    result = []
    for t in all_tools:
        if t.category == "feishu" and not has_feishu:
            continue
        if (t.config or {}).get("okr_agent_only") and not is_system_agent:
            continue
        tid = str(t.id)
        at = assignments.get(tid)
        if not _tool_record_visible_to_agent(t, agent_obj.tenant_id, assignments):
            continue
        enabled = at.enabled if at else t.is_default
        result.append(
            {
                "id": tid,
                "name": t.name,
                "display_name": t.display_name,
                "description": t.description,
                "type": t.type,
                "category": t.category,
                "icon": t.icon,
                "enabled": enabled,
                "is_default": t.is_default,
                "mcp_server_name": t.mcp_server_name,
                "mcp_server_url": t.mcp_server_url,
                "source": t.source,
            }
        )
    return result


@router.put("/agents/{agent_id}")
async def update_agent_tools(
    agent_id: uuid.UUID, updates: list[AgentToolUpdate], current_user: UserRecord = Depends(get_current_user)
):
    """Update tool assignments for an agent."""
    _agent, access_level = await check_agent_access(current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=403, detail="Manage access required")
    agent_obj = await _load_agent_for_tool_scope(agent_id)
    assignments = await _load_agent_tool_assignments(agent_id)
    for u in updates:
        tool_id = uuid.UUID(u.tool_id)
        tool_obj = await tool_dao.get(tool_id)
        if not tool_obj or not _tool_record_visible_to_agent(tool_obj, agent_obj.tenant_id, assignments):
            raise HTTPException(status_code=404, detail="Tool not found")

        if tool_obj.category == "system" and not u.enabled:
            continue

        at = await agent_tool_dao.get_assignment(agent_id, tool_id)
        if at:
            await agent_tool_dao.update(db_obj=at, obj_in={"enabled": u.enabled})
        else:
            await agent_tool_dao.create(obj_in={"agent_id": agent_id, "tool_id": tool_id, "enabled": u.enabled})
    return {"ok": True}


# ─── MCP Server Testing ────────────────────────────────────
class MCPTestRequest(BaseModel):
    server_url: str
    # Optional standalone API Key. If provided, it is sent as
    # 'Authorization: Bearer {api_key}' and is NOT embedded in the URL.
    api_key: str | None = None


@router.post("/test-mcp")
async def check_mcp_connection(
    data: MCPTestRequest,
    current_user: UserRecord = Depends(get_current_user),
):
    """Test connection to an MCP server and list available tools.

    Supports two authentication modes:
    - URL-embedded key (e.g. ?tavilyApiKey=xxx) - include in server_url.
    - Bearer token - pass via api_key field; sent as Authorization header.
    """
    from app.services.mcp_client import MCPClient
    from app.services.trigger_runtime.evaluator import is_private_url

    if is_private_url(data.server_url):
        raise HTTPException(status_code=400, detail="MCP server URL is not allowed")

    try:
        client = MCPClient(data.server_url, api_key=data.api_key or None)
        tools = await client.list_tools()
        return {"ok": True, "tools": tools}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


# ─── MCP Server-level Credential Management ────────────────
class MCPServerUpdate(BaseModel):
    server_name: str  # Identifies which server's tools to update
    server_url: str  # New MCP server URL (may contain embedded key)
    api_key: str | None = None  # Optional standalone Bearer key
    # Target tenant (platform admins may manage another company's tools)
    tenant_id: str | None = None


@router.put("/mcp-server")
async def update_mcp_server(data: MCPServerUpdate, current_user: UserRecord = Depends(get_current_user)):
    """Bulk-update the Server URL and API Key for all tools from an MCP server.

    All tools sharing the same mcp_server_name under the target tenant are
    updated atomically. The API Key is stored encrypted in tool.config so
    the agent runner can resolve it at execution time without re-configuring
    each tool individually.

    Authentication priority at runtime (handled by MCPClient):
    1. tool.config['api_key'] - sent as Authorization: Bearer header.
    2. URL query param (e.g. ?tavilyApiKey=xxx) - extracted from the URL
       and converted to Bearer by MCPClient automatically.
    """
    _require_catalog_manager(current_user)
    target_tenant_id = _resolve_target_tenant_id(current_user, data.tenant_id)

    tools = await tool_dao.list_by_mcp_server(data.server_name, target_tenant_id)
    if not tools:
        raise HTTPException(
            status_code=404,
            detail=f"No tools found for server '{data.server_name}'",
        )

    for tool in tools:
        updates: dict[str, Any] = {"mcp_server_url": data.server_url}
        if data.api_key is not None:
            current_config = dict(tool.config or {})
            current_config["api_key"] = data.api_key
            updates["config"] = _encrypt_sensitive_fields(current_config, tool.config_schema)
        await tool_dao.update(db_obj=tool, obj_in=updates)

    return {"ok": True, "updated": len(tools)}


# ─── Agent-installed Tools Management (admin) ───────────────


@router.get("/agent-installed")
async def list_agent_installed_tools(
    tenant_id: str | None = None, current_user: UserRecord = Depends(get_current_user)
):
    """Admin endpoint: list user-installed tools scoped by tenant."""
    tid = tenant_id or (str(current_user.tenant_id) if current_user.tenant_id else None)
    rows = await agent_tool_dao.list_agent_installed(tid)
    return [
        {
            "agent_tool_id": str(row["agent_tool_id"]),
            "agent_id": str(row["agent_id"]),
            "tool_id": str(row["tool_id"]),
            "tool_name": row["tool_name"],
            "tool_display_name": row["tool_display_name"],
            "description": row["description"],
            "type": row["type"],
            "category": row["category"],
            "source": row["source"],
            "mcp_server_name": row["mcp_server_name"],
            "mcp_server_url": row["mcp_server_url"],
            "mcp_tool_name": row["mcp_tool_name"],
            "installed_by_agent_id": str(row["installed_by_agent_id"]) if row["installed_by_agent_id"] else None,
            "installed_by_agent_name": row["installed_by_agent_name"],
            "enabled": row["enabled"],
            "configured": bool(row["config"] and len(row["config"]) > 0),
            "installed_at": row["installed_at"].isoformat() if row["installed_at"] else None,
        }
        for row in rows
    ]


@router.delete("/agent-tool/{agent_tool_id}")
async def delete_agent_tool(agent_tool_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)):
    """Admin: remove an agent-tool assignment. Also deletes the tool record if no other agents use it."""
    at = await agent_tool_dao.get(agent_tool_id)
    if not at:
        raise HTTPException(status_code=404, detail="Agent tool assignment not found")
    tool_id = at.tool_id
    await agent_tool_dao.delete(id=agent_tool_id)
    remaining = await agent_tool_dao.list_for_tool(tool_id)
    if not remaining:
        tool = await tool_dao.get(tool_id)
        if tool and tool.type == "mcp":
            await tool_dao.delete(id=tool_id)
    return {"ok": True}


# ─── Per-Agent Tool Config ───────────────────────────────────


class AgentToolConfigUpdate(BaseModel):
    config: JsonObject


@router.get("/agents/{agent_id}/tool-config/{tool_id}")
async def get_agent_tool_config(
    agent_id: uuid.UUID, tool_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user), db=None
):
    """Get merged tool config (global defaults + agent overrides) and config_schema.

    Both configs are decrypted before returning. Global sensitive fields are
    masked so the frontend can show a key is configured without exposing it.
    """
    await check_agent_access(current_user, agent_id)
    tool = await tool_dao.get(tool_id)
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")
    agent = await _load_agent_for_tool_scope(agent_id)
    at = await agent_tool_dao.get_assignment(agent_id, tool_id)

    schema = tool.config_schema
    raw_global = await get_tool_company_config(db, tool, agent.tenant_id)
    raw_agent = _decrypt_sensitive_fields(at.config if at else {}, schema)

    masked_global = mask_sensitive_fields(raw_global, schema)
    masked_agent = mask_sensitive_fields(raw_agent or {}, schema)
    merged = {**masked_global, **masked_agent}
    return {
        "global_config": masked_global,
        "agent_config": masked_agent,
        "merged_config": merged,
        "config_schema": tool.config_schema or {},
    }


@router.put("/agents/{agent_id}/tool-config/{tool_id}")
async def update_agent_tool_config(
    agent_id: uuid.UUID,
    tool_id: uuid.UUID,
    data: AgentToolConfigUpdate,
    current_user: UserRecord = Depends(get_current_user),
):
    """Save per-agent config override for a tool."""
    _agent, access_level = await check_agent_access(current_user, agent_id)
    if access_level != "manage" and current_user.role not in {"platform_admin", "org_admin"}:
        raise HTTPException(status_code=403, detail="Manage access required")
    # Network / proxy settings affect untrusted code egress; admin-only.
    _network_proxy_keys = ("allow_network", "http_proxy", "https_proxy", "no_proxy")
    if any(key in data.config for key in _network_proxy_keys) and current_user.role not in (
        "platform_admin",
        "org_admin",
    ):
        raise HTTPException(
            status_code=403,
            detail="Only platform admin or organization admin can modify network or proxy settings",
        )

    tool_for_schema = await tool_dao.get(tool_id)
    encrypted_config = _encrypt_sensitive_fields(
        data.config, tool_for_schema.config_schema if tool_for_schema else None
    )

    at = await agent_tool_dao.get_assignment(agent_id, tool_id)
    if at:
        await agent_tool_dao.update(db_obj=at, obj_in={"config": encrypted_config})
    else:
        await agent_tool_dao.create(
            obj_in={
                "agent_id": agent_id,
                "tool_id": tool_id,
                "enabled": True,
                "config": encrypted_config,
            }
        )
    return {"ok": True}


@router.get("/agents/{agent_id}/with-config")
async def get_agent_tools_with_config(
    agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user), db=None
):
    """Get agent's enabled tools with per-agent config info and config_schema for settings UI.

    Both global_config and agent_config are decrypted before returning.
    For global_config, sensitive fields are masked (e.g. "sk-****abcd") so the
    frontend can show that a company key is configured without exposing it.

    Special handling: some tools (Jina) store their API key in system_settings
    rather than Tool.config. We resolve those as part of the global config so
    the agent-level UI can show the inherited key hint.
    """
    from app.services.agent_tools import _agent_has_feishu

    has_feishu = await _agent_has_feishu(agent_id)

    agent_obj2 = await _load_agent_for_tool_scope(agent_id)
    is_system_agent2 = bool(agent_obj2 and agent_obj2.is_system)

    assignments = await _load_agent_tool_assignments(agent_id)
    all_tools = await tool_dao.list_enabled_visible(
        agent_tenant_id=agent_obj2.tenant_id,
        assigned_tool_ids=[uuid.UUID(tid) for tid in assignments],
    )

    system_keys_cache: dict[str, str] = {}
    system_settings_tool_map = {
        "jina_search": ("jina_api_key", "api_key"),
        "jina_read": ("jina_api_key", "api_key"),
    }

    result = []
    for t in all_tools:
        if t.category == "feishu" and not has_feishu:
            continue
        if (t.config or {}).get("okr_agent_only") and not is_system_agent2:
            continue
        tid = str(t.id)
        at = assignments.get(tid)
        if not _tool_record_visible_to_agent(t, agent_obj2.tenant_id, assignments):
            continue
        enabled = at.enabled if at else t.is_default

        raw_global = await get_tool_company_config(db, t, agent_obj2.tenant_id)

        if t.name in system_settings_tool_map and not raw_global.get("api_key"):
            ss_key, ss_field = system_settings_tool_map[t.name]
            if ss_key not in system_keys_cache:
                try:
                    value = await system_setting_dao.get_value(ss_key, {})
                    system_value = value.get(ss_field, "") if isinstance(value, dict) else ""
                    system_keys_cache[ss_key] = system_value if isinstance(system_value, str) else ""
                except Exception:
                    system_keys_cache[ss_key] = ""
            if system_keys_cache[ss_key]:
                raw_global["api_key"] = system_keys_cache[ss_key]

        raw_agent = _decrypt_sensitive_fields((at.config if at else {}) or {}, t.config_schema)
        masked_global = mask_sensitive_fields(raw_global, t.config_schema)

        result.append(
            {
                "id": tid,
                "agent_tool_id": str(at.id) if at else None,
                "name": t.name,
                "display_name": t.display_name,
                "description": t.description,
                "type": t.type,
                "category": t.category,
                "icon": t.icon,
                "enabled": enabled,
                "is_default": t.is_default,
                "mcp_server_name": t.mcp_server_name,
                "mcp_server_url": t.mcp_server_url,
                "config_schema": t.config_schema or {},
                "global_config": masked_global,
                "agent_config": raw_agent,
                "source": t.source,
            }
        )
    return result


# ─── Email Connection Testing ──────────────────────────────


class EmailTestRequest(BaseModel):
    config: EmailConfig


@router.post("/test-email")
async def check_email_connection(
    data: EmailTestRequest,
    current_user: UserRecord = Depends(get_current_user),
):
    """Test IMAP and SMTP email connections with provided config."""
    from app.services.email_service import test_connection

    try:
        return await test_connection(data.config)
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


@router.get("/email-providers")
async def get_email_providers(
    current_user: UserRecord = Depends(get_current_user),
):
    """Get list of supported email provider presets with help text."""
    from app.services.email_service import EMAIL_PROVIDERS

    return {
        key: {
            "label": p["label"],
            "help_url": p.get("help_url", ""),
            "help_text": p.get("help_text", ""),
        }
        for key, p in EMAIL_PROVIDERS.items()
    }


# ─── Tool Category Sharing Config (Generic ChannelConfig) ───


@router.get("/agents/{agent_id}/category-config/{category}")
async def get_category_config(
    agent_id: uuid.UUID, category: str, current_user: UserRecord = Depends(get_current_user), db=None
):
    """Get shared configuration for a tool category.

    Returns both global_config (company-level, from Tool.config) and
    agent_config (agent-level override, from ChannelConfig) separately.
    Sensitive fields in global_config are masked for display.
    Company-level values always take precedence at runtime.
    """
    from app.core.permissions import check_agent_access

    agent, _ = await check_agent_access(current_user, agent_id)

    primary_tool_name = CATEGORY_CONFIG_PRIMARY_TOOL.get(category)
    assignments = await _load_agent_tool_assignments(agent_id)
    all_cat_tools = await tool_dao.list_enabled_by_category_visible(
        category,
        agent_tenant_id=agent.tenant_id,
        assigned_tool_ids=[uuid.UUID(tid) for tid in assignments],
        primary_tool_name=primary_tool_name,
    )
    raw_global: JsonObject = {}
    cat_schema: dict | None = None
    for ct in all_cat_tools:
        company_config = await get_tool_company_config(db, ct, agent.tenant_id)
        if company_config:
            cat_schema = ct.config_schema
            raw_global = company_config
            break

    masked_global = mask_sensitive_fields(raw_global, cat_schema)

    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type=category)

    config_id = None
    is_configured = bool(raw_global) or config is not None
    raw_agent: JsonObject = {}

    if config:
        config_id = str(config.id)
        full_agent: JsonObject = {
            "api_key": config.app_secret,
            **(config.extra_config or {}),
        }
        raw_agent = _decrypt_sensitive_fields(full_agent)
        raw_agent = {k: v for k, v in raw_agent.items() if v is not None}

    effective_config = {**raw_global, **raw_agent}

    return {
        "id": config_id,
        "agent_id": str(agent_id),
        "category": category,
        "is_configured": is_configured,
        "config": effective_config,
        "global_config": masked_global,
        "agent_config": raw_agent,
    }


@router.post("/agents/{agent_id}/category-config/{category}")
async def update_category_config(
    agent_id: uuid.UUID, category: str, data: CategoryConfigUpdate, current_user: UserRecord = Depends(get_current_user)
):
    """Update or create shared configuration for a tool category."""
    from app.core.permissions import check_agent_access, is_agent_creator

    agent, _ = await check_agent_access(current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can configure category")

    encrypted_config = _encrypt_sensitive_fields(data.config)
    secret_value = (
        encrypted_config.get("api_key") or encrypted_config.get("api_secret") or encrypted_config.get("app_secret")
    )
    app_secret = secret_value if isinstance(secret_value, str) else None
    extra: JsonObject = {
        key: value for key, value in encrypted_config.items() if key not in ("api_key", "api_secret", "app_secret")
    }

    existing = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type=category)
    if existing:
        updates: dict[str, Any] = {
            "extra_config": {**(existing.extra_config or {}), **extra},
            "is_configured": True,
        }
        if app_secret:
            updates["app_secret"] = app_secret
        await channel_config_dao.update(db_obj=existing, obj_in=updates)
    else:
        await channel_config_dao.create(
            obj_in={
                "agent_id": agent_id,
                "channel_type": category,
                "app_id": category,
                "app_secret": app_secret,
                "extra_config": extra,
                "is_configured": True,
            }
        )

    if category == "atlassian" and app_secret:
        from app.api.atlassian import _sync_atlassian_tools_for_agent
        from app.api.background_tasks import schedule_background_task

        schedule_background_task(_sync_atlassian_tools_for_agent(agent_id, app_secret), "sync Atlassian tools")

    return {"ok": True}


@router.delete("/agents/{agent_id}/category-config/{category}", status_code=204)
async def delete_category_config(
    agent_id: uuid.UUID, category: str, current_user: UserRecord = Depends(get_current_user)
):
    """Remove shared configuration for a tool category."""
    from app.core.permissions import check_agent_access, is_agent_creator

    agent, _ = await check_agent_access(current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can remove config")

    existing = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type=category)
    if existing:
        await channel_config_dao.delete(id=existing.id)


@router.post("/agents/{agent_id}/category-config/{category}/test")
async def check_category_config(
    agent_id: uuid.UUID, category: str, current_user: UserRecord = Depends(get_current_user)
):
    """Test connectivity for a tool category."""
    if category == "atlassian":
        from app.api.atlassian import check_atlassian_channel

        return await check_atlassian_channel(agent_id, current_user)
    if category == "agentbay":
        from app.services.agentbay_client import test_agentbay_channel

        return await test_agentbay_channel(agent_id, current_user)

    return {"ok": True, "message": f"Settings for {category} saved."}
