"""Seed builtin tools into the database on startup."""

from typing import Any, TypedDict, TypeIs
from uuid import UUID

from app.core.json_types import JsonObject, JsonValue
from app.core.logging import logger
from app.core.tool_types import ToolConfigSchema, ToolParameterSchema
from app.dao import agent_dao, agent_tool_dao, tenant_dao, tool_dao
from app.db.session import connection_ctx
from app.services import tool_definitions
from app.services.tool_config import get_tenant_tool_config, meaningful_config, set_tenant_tool_config

AGENTBAY_TOOLS = tool_definitions.AGENTBAY_TOOLS
BUILTIN_TOOLS = tool_definitions.BUILTIN_TOOLS
DEPLOY_BUILTIN_TOOLS = tool_definitions.DEPLOY_BUILTIN_TOOLS
OKR_BUILTIN_TOOLS = tool_definitions.OKR_BUILTIN_TOOLS

SYNC_IS_DEFAULT_TOOL_NAMES = {
    "finish",
    "read_webpage",
    "duckduckgo_search",
    "jina_search",
    "jina_read",
    "update_objective",
    # AgentBay tools should NOT be is_default=True. Older seeder versions may
    # have set them to True; include them here so the seeder corrects the DB.
    "agentbay_browser_navigate",
    "agentbay_browser_screenshot",
    "agentbay_browser_save_screenshot",
    "agentbay_browser_click",
    "agentbay_browser_type",
    "agentbay_browser_extract",
    "agentbay_browser_observe",
    "agentbay_browser_login",
    "agentbay_code_execute",
    "agentbay_code_write_file",
    "agentbay_code_read_file",
    "agentbay_code_edit_file",
    "agentbay_command_exec",
    "agentbay_computer_screenshot",
    "agentbay_computer_save_screenshot",
    "agentbay_computer_click",
    "agentbay_computer_precision_screenshot",
    "agentbay_computer_input_text",
    "agentbay_computer_press_keys",
    "agentbay_computer_scroll",
    "agentbay_computer_move_mouse",
    "agentbay_computer_drag_mouse",
    "agentbay_computer_get_installed_apps",
    "agentbay_computer_start_app",
    "agentbay_computer_list_windows",
    "agentbay_computer_close_window",
    "agentbay_computer_dismiss_dialog",
    "agentbay_file_transfer",
}

LEGACY_IMAGE_TOOL_MODEL_DEFAULTS = {
    "generate_image_siliconflow": "black-forest-labs/FLUX.1-schnell",
    "generate_image_openai": "dall-e-3",
    "generate_image_google": "gemini-2.5-flash-image",
}


class AtlassianRovoConfigField(TypedDict):
    key: str
    label: str
    type: str
    default: str
    placeholder: str
    description: str


class AtlassianRovoConfigSchema(TypedDict):
    fields: list[AtlassianRovoConfigField]


class AtlassianRovoConfigTool(TypedDict):
    name: str
    display_name: str
    description: str
    category: str
    icon: str
    is_default: bool
    parameters_schema: ToolParameterSchema
    config: JsonObject
    config_schema: AtlassianRovoConfigSchema


class BuiltinToolDefinition(TypedDict):
    name: str
    display_name: str
    description: str
    category: str
    icon: str
    is_default: bool
    parameters_schema: ToolParameterSchema
    config: JsonObject
    config_schema: ToolConfigSchema


def _is_json_value(value: object) -> TypeIs[JsonValue]:
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, list):
        return all(_is_json_value(item) for item in list[object](value))
    return _is_json_object(value)


def _is_json_object(value: object) -> TypeIs[JsonObject]:
    if not isinstance(value, dict):
        return False
    mapping: dict[object, object] = dict(value)
    return all(isinstance(key, str) and _is_json_value(item) for key, item in mapping.items())


def _is_tool_parameter_schema(value: object) -> TypeIs[ToolParameterSchema]:
    if not _is_json_object(value):
        return False
    schema_type = value.get("type")
    properties = value.get("properties")
    required = value.get("required")
    return (
        isinstance(schema_type, str)
        and isinstance(properties, dict)
        and all(_is_json_object(property_schema) for property_schema in properties.values())
        and (required is None or (isinstance(required, list) and all(isinstance(item, str) for item in required)))
    )


def _is_tool_config_schema(value: object) -> TypeIs[ToolConfigSchema]:
    if not _is_json_object(value):
        return False
    fields = value.get("fields")
    if fields is None:
        return True
    if not isinstance(fields, list):
        return False
    return all(
        _is_json_object(field)
        and (field.get("key") is None or isinstance(field.get("key"), str))
        and (field.get("type") is None or isinstance(field.get("type"), str))
        for field in fields
    )


def _is_builtin_tool_definition(value: object) -> TypeIs[BuiltinToolDefinition]:
    if not _is_json_object(value):
        return False
    return (
        isinstance(value.get("name"), str)
        and isinstance(value.get("display_name"), str)
        and isinstance(value.get("description"), str)
        and isinstance(value.get("category"), str)
        and isinstance(value.get("icon"), str)
        and isinstance(value.get("is_default"), bool)
        and _is_tool_parameter_schema(value.get("parameters_schema"))
        and _is_json_object(value.get("config"))
        and _is_tool_config_schema(value.get("config_schema"))
    )


def _global_builtin_config(tool_data: BuiltinToolDefinition) -> JsonObject:
    """Return config safe to store on the global builtin Tool row."""
    # Builtin tools specify defaults (like 'allow_network': True) in their 'config' dict.
    # The actual sensitive data defaults are empty strings ("") so this is safe to store globally.
    config = tool_data.get("config")
    if isinstance(config, dict):
        return config
    return {}


async def seed_builtin_tools():
    """Insert or update builtin tools in the database."""
    async with connection_ctx():
        # Legacy rename: older environments persisted this tool as
        # `send_web_message`. Rename or merge it in-place so agents keep the
        # same assignment after the first startup on the new version.
        old_name = "send_web_message"
        new_name = "send_platform_message"
        old_tool = await tool_dao.get_by_name(old_name)
        new_tool = await tool_dao.get_by_name(new_name)
        if old_tool and not new_tool:
            _ = await tool_dao.update(db_obj=old_tool, obj_in={"name": new_name})
            logger.info(f"[ToolSeeder] Renamed builtin tool: {old_name} -> {new_name}")
        elif old_tool and new_tool:
            await agent_tool_dao.reassign_tool(from_tool_id=old_tool.id, to_tool_id=new_tool.id)
            _ = await tool_dao.delete(id=old_tool.id)
            logger.info(f"[ToolSeeder] Merged legacy builtin tool into {new_name}")

        new_tool_ids: list[UUID] = []
        for t in BUILTIN_TOOLS:
            if not _is_builtin_tool_definition(t):
                raise ValueError("Builtin tool definition has an invalid seed schema")
            seed_config = _global_builtin_config(t)
            existing = await tool_dao.get_by_name(t["name"])
            if not existing:
                tool = await tool_dao.create(
                    obj_in={
                        "name": t["name"],
                        "display_name": t["display_name"],
                        "description": t["description"],
                        "type": "builtin",
                        "category": t["category"],
                        "icon": t["icon"],
                        "is_default": t["is_default"],
                        "parameters_schema": t.get("parameters_schema", {"type": "object", "properties": {}}),
                        "config": seed_config,
                        "config_schema": t.get("config_schema", {}),
                        "source": "builtin",
                    }
                )
                if t["is_default"]:
                    new_tool_ids.append(tool.id)
                logger.info(f"[ToolSeeder] Created builtin tool: {t['name']}")
            else:
                updated_fields: list[str] = []
                updates: dict[str, Any] = {}
                if existing.category != t["category"]:
                    updates["category"] = t["category"]
                    updated_fields.append("category")
                if existing.description != t["description"]:
                    updates["description"] = t["description"]
                    updated_fields.append("description")
                if existing.display_name != t["display_name"]:
                    updates["display_name"] = t["display_name"]
                    updated_fields.append("display_name")
                if existing.icon != t["icon"]:
                    updates["icon"] = t["icon"]
                    updated_fields.append("icon")
                if t["name"] in SYNC_IS_DEFAULT_TOOL_NAMES and existing.is_default != t["is_default"]:
                    updates["is_default"] = t["is_default"]
                    updated_fields.append("is_default")
                config: dict[str, Any] = dict(existing.config or {})
                config_schema = dict(existing.config_schema or {})
                if t.get("config_schema") and config_schema != t["config_schema"]:
                    updates["config_schema"] = t["config_schema"]
                    updated_fields.append("config_schema")
                    if seed_config:
                        config = {**seed_config, **config}
                        updates["config"] = config
                        updated_fields.append("config")
                if not config and seed_config:
                    updates["config"] = seed_config
                    updated_fields.append("config")
                    config = dict(seed_config)
                elif seed_config and config != seed_config:
                    merged: dict[str, Any] = {**seed_config, **config}
                    if merged != config:
                        updates["config"] = merged
                        updated_fields.append("config")
                        config = merged
                legacy_model = LEGACY_IMAGE_TOOL_MODEL_DEFAULTS.get(t["name"])
                if legacy_model and config == {
                    "model": legacy_model,
                    "api_key": "",
                    "base_url": "",
                }:
                    updates["config"] = {"model": "", "api_key": "", "base_url": ""}
                    updated_fields.append("config")
                if existing.parameters_schema != t["parameters_schema"]:
                    updates["parameters_schema"] = t["parameters_schema"]
                    updated_fields.append("parameters_schema")
                if updates:
                    _ = await tool_dao.update(db_obj=existing, obj_in=updates)
                    logger.info(f"[ToolSeeder] Updated {', '.join(updated_fields)}: {t['name']}")

        if new_tool_ids:
            agent_ids = await agent_dao.list_all_ids()
            for agent_id in agent_ids:
                for tool_id in new_tool_ids:
                    _ = await agent_tool_dao.ensure_enabled(agent_id, tool_id)
            logger.info(f"[ToolSeeder] Auto-assigned {len(new_tool_ids)} new tools to {len(agent_ids)} agents")

        await _auto_assign_helpers(
            anchor_names=[
                "agentbay_computer_screenshot",
                "agentbay_computer_precision_screenshot",
                "agentbay_computer_click",
                "agentbay_computer_get_active_window",
                "agentbay_computer_activate_window",
            ],
            helper_names=[
                "agentbay_computer_precision_screenshot",
                "agentbay_computer_save_screenshot",
                "agentbay_computer_list_windows",
                "agentbay_computer_close_window",
                "agentbay_computer_dismiss_dialog",
            ],
            label="computer",
        )
        await _auto_assign_helpers(
            anchor_names=["agentbay_browser_navigate", "agentbay_browser_screenshot"],
            helper_names=["agentbay_browser_save_screenshot"],
            label="browser",
        )
        await _auto_assign_helpers(
            anchor_names=["agentbay_code_execute", "agentbay_command_exec", "agentbay_file_transfer"],
            helper_names=["agentbay_code_write_file", "agentbay_code_read_file", "agentbay_code_edit_file"],
            label="code",
        )

        for obsolete_name in ("bing_search", "manage_tasks"):
            obsolete = await tool_dao.get_by_name(obsolete_name)
            if obsolete:
                _ = await tool_dao.delete(id=obsolete.id)
                logger.info(f"[ToolSeeder] Removed obsolete tool: {obsolete_name}")

        first_tenant = await tenant_dao.get_first_by_created_at()
        if first_tenant:
            migrated = 0
            for tool in await tool_dao.list_by_source("builtin"):
                if not (tool.config_schema or {}).get("fields"):
                    continue
                legacy_config = meaningful_config(tool.config or {})
                if not legacy_config:
                    continue
                config_schema = tool.config_schema if _is_tool_config_schema(tool.config_schema) else None
                existing_setting = await get_tenant_tool_config(
                    None,
                    first_tenant.id,
                    tool.name,
                    config_schema,
                )
                if not existing_setting:
                    await set_tenant_tool_config(
                        None,
                        first_tenant.id,
                        tool.name,
                        legacy_config,
                        config_schema,
                    )
                    migrated += 1
                schema_fields = (tool.config_schema or {}).get("fields", [])
                sensitive_keys = {f["key"] for f in schema_fields if f.get("type") == "password"}
                clean_config = {key: value for key, value in (tool.config or {}).items() if key not in sensitive_keys}
                if clean_config != tool.config:
                    _ = await tool_dao.update(db_obj=tool, obj_in={"config": clean_config})
            if migrated:
                logger.info(
                    f"[ToolSeeder] Migrated {migrated} legacy builtin tool config(s) "
                    + f"to tenant_settings for tenant {first_tenant.id}"
                )

    logger.info("[ToolSeeder] Builtin tools seeded")


async def _auto_assign_helpers(*, anchor_names: list[str], helper_names: list[str], label: str) -> None:
    anchor_tool_ids = list(await tool_dao.list_ids_by_names(anchor_names))
    helper_tools = list(await tool_dao.list_by_names(helper_names))
    if not anchor_tool_ids or not helper_tools:
        return
    enabled_agent_ids = await agent_tool_dao.list_agent_ids_with_enabled_tools(anchor_tool_ids)
    assigned_count = 0
    for agent_id in enabled_agent_ids:
        for helper_tool in helper_tools:
            if await agent_tool_dao.ensure_enabled(agent_id, helper_tool.id):
                assigned_count += 1
    if assigned_count:
        logger.info(
            f"[ToolSeeder] Auto-assigned {assigned_count} AgentBay {label} helper tool(s) "
            + f"to {len(enabled_agent_ids)} agent(s)"
        )


async def clean_orphaned_mcp_tools():
    """Clean up orphan MCP tools that lost all their AgentTool assignments.

    This happens when an Agent is deleted (cascade deletes AgentTool) but the
    shared Tool record remains. We run this periodically/on-startup to prevent
    the database from filling up with abandoned tool records.
    """
    async with connection_ctx():
        assigned_ids = await agent_tool_dao.list_distinct_tool_ids()
        deleted_count = await tool_dao.delete_orphan_mcp_tools(assigned_ids)
        if deleted_count > 0:
            logger.info(f"[ToolSeeder] Cleaned up {deleted_count} orphaned MCP tools")


# ── Atlassian Rovo MCP Server Integration ──────────────────────────────────

ATLASSIAN_ROVO_MCP_URL = "https://mcp.atlassian.com/v1/mcp"

ATLASSIAN_ROVO_CONFIG_TOOL: AtlassianRovoConfigTool = {
    "name": "atlassian_rovo",
    "display_name": "Atlassian Rovo (Jira / Confluence / Compass)",
    "description": (
        "Connect to Atlassian Rovo MCP Server to access Jira, Confluence, and Compass. "
        + "Configure your API key to enable Jira issue management, Confluence page creation, "
        + "and Compass component queries."
    ),
    "category": "atlassian",
    "icon": "🔷",
    "is_default": False,
    "parameters_schema": {"type": "object", "properties": {}},
    "config": {"api_key": ""},
    "config_schema": {
        "fields": [
            {
                "key": "api_key",
                "label": "Atlassian API Key",
                "type": "password",
                "default": "",
                "placeholder": "ATSTT3x... (service account key) or Basic base64(email:token)",
                "description": (
                    "Service account API key (Bearer) or base64-encoded email:api_token (Basic). "
                    + "Get your API key from id.atlassian.com/manage-profile/security/api-tokens"
                ),
            },
        ]
    },
}


async def seed_atlassian_rovo_config():
    """Ensure the Atlassian Rovo platform config tool exists in the database.

    If the env var ATLASSIAN_API_KEY is set, it will be written into the tool config
    so the platform is immediately ready without manual UI setup.
    """
    import os

    env_key = os.environ.get("ATLASSIAN_API_KEY", "").strip()

    async with connection_ctx():
        t = ATLASSIAN_ROVO_CONFIG_TOOL
        config_schema = t["config_schema"]
        if not _is_tool_config_schema(config_schema):
            raise ValueError("Atlassian Rovo tool has an invalid config schema")
        existing = await tool_dao.get_by_name(t["name"])
        if not existing:
            initial_config: JsonObject = dict(t["config"])
            if env_key:
                initial_config["api_key"] = env_key
            _ = await tool_dao.create(
                obj_in={
                    "name": t["name"],
                    "display_name": t["display_name"],
                    "description": t["description"],
                    "type": "mcp_config",
                    "category": t["category"],
                    "icon": t["icon"],
                    "is_default": t["is_default"],
                    "parameters_schema": t["parameters_schema"],
                    "config": initial_config,
                    "config_schema": config_schema,
                    "mcp_server_url": ATLASSIAN_ROVO_MCP_URL,
                    "mcp_server_name": "Atlassian Rovo",
                    "source": "admin",
                }
            )
            logger.info("[ToolSeeder] Created Atlassian Rovo config tool")
            return

        updates: dict[str, Any] = {}
        if existing.config_schema != config_schema:
            updates["config_schema"] = config_schema
        if existing.mcp_server_url != ATLASSIAN_ROVO_MCP_URL:
            updates["mcp_server_url"] = ATLASSIAN_ROVO_MCP_URL
        if env_key and (not existing.config or not existing.config.get("api_key")):
            updates["config"] = {**(existing.config or {}), "api_key": env_key}
        if updates:
            _ = await tool_dao.update(db_obj=existing, obj_in=updates)
            logger.info("[ToolSeeder] Updated Atlassian Rovo config tool")


async def get_atlassian_api_key() -> str:
    """Read the Atlassian API key from the platform config tool."""
    tool = await tool_dao.get_by_name("atlassian_rovo")
    if tool and tool.config:
        api_key = tool.config.get("api_key")
        if isinstance(api_key, str):
            return api_key
    return ""
