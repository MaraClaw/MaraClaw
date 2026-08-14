import uuid
from typing import NotRequired, TypedDict

from app.core.json_types import JsonObject, JsonValue
from app.core.logging import logger

ATLASSIAN_MCP_URL = "https://mcp.atlassian.com/v1/mcp"


class AtlassianToolParameterSchema(TypedDict):
    """OpenAI-style parameter schema for Atlassian MCP tools."""

    type: str
    properties: dict[str, JsonObject]
    required: NotRequired[list[str]]


class AtlassianToolPreview(TypedDict):
    name: JsonValue
    description: str


class AtlassianToolDefinition(TypedDict):
    name: str
    description: str
    parameters_schema: AtlassianToolParameterSchema


def _preview_atlassian_tools(tools: list[JsonObject]) -> list[AtlassianToolPreview]:
    previews: list[AtlassianToolPreview] = []
    for tool in tools[:10]:
        description = tool.get("description", "")
        if not isinstance(description, str):
            raise TypeError("Atlassian tool description must be a string")
        previews.append({"name": tool["name"], "description": description[:100]})
    return previews


def _normalize_atlassian_tool(mcp_tool: JsonObject) -> AtlassianToolDefinition | None:
    raw_name = mcp_tool.get("name", "")
    description = mcp_tool.get("description", "")
    if not isinstance(raw_name, str) or not raw_name or not isinstance(description, str):
        return None

    input_schema = mcp_tool.get("inputSchema")
    default_schema: AtlassianToolParameterSchema = {"type": "object", "properties": {}}
    if not isinstance(input_schema, dict):
        return {"name": raw_name, "description": description[:500], "parameters_schema": default_schema}

    schema_type = input_schema.get("type")
    raw_properties = input_schema.get("properties")
    if not isinstance(schema_type, str) or not isinstance(raw_properties, dict):
        return {"name": raw_name, "description": description[:500], "parameters_schema": default_schema}

    properties: dict[str, JsonObject] = {}
    for property_name, property_schema in raw_properties.items():
        if not isinstance(property_name, str) or not isinstance(property_schema, dict):
            return {"name": raw_name, "description": description[:500], "parameters_schema": default_schema}
        properties[property_name] = property_schema

    parameters_schema: AtlassianToolParameterSchema = {"type": schema_type, "properties": properties}
    required = input_schema.get("required")
    if required is not None:
        if not isinstance(required, list):
            return {"name": raw_name, "description": description[:500], "parameters_schema": default_schema}
        required_fields: list[str] = []
        for field in required:
            if not isinstance(field, str):
                return {"name": raw_name, "description": description[:500], "parameters_schema": default_schema}
            required_fields.append(field)
        parameters_schema["required"] = required_fields

    return {"name": raw_name, "description": description[:500], "parameters_schema": parameters_schema}


async def _sync_atlassian_tools_for_agent(agent_id: uuid.UUID, api_key: str) -> None:
    """Connect to Atlassian Rovo MCP and ensure all tools are seeded + assigned to this agent.

    Discovers tools from the MCP server, creates Tool records if needed,
    and creates AgentTool assignments for this specific agent.
    """
    from app.dao.tool_dao import agent_tool_dao, tool_dao
    from app.services.mcp_client import MCPClient

    logger.info(f"[AtlassianChannel] Syncing tools for agent {agent_id} ...")
    try:
        client = MCPClient(ATLASSIAN_MCP_URL, api_key=api_key)
        tools_discovered = await client.list_tools()
    except Exception as e:
        logger.error(f"[AtlassianChannel] Could not list tools: {e}")
        return

    if not tools_discovered:
        logger.warning("[AtlassianChannel] No tools returned from Atlassian MCP")
        return

    logger.info(f"[AtlassianChannel] Found {len(tools_discovered)} tools, assigning to agent {agent_id}")

    assigned = 0
    for mcp_tool in tools_discovered:
        tool_definition = _normalize_atlassian_tool(mcp_tool)
        if tool_definition is None:
            continue

        raw_name = tool_definition["name"]
        tool_name = f"atlassian_rovo_{raw_name}"
        tool_desc = tool_definition["description"]
        tool_schema = tool_definition["parameters_schema"]

        if "jira" in raw_name.lower() or "issue" in raw_name.lower():
            icon = "🔵"
        elif "confluence" in raw_name.lower() or "page" in raw_name.lower():
            icon = "📘"
        elif "compass" in raw_name.lower() or "component" in raw_name.lower():
            icon = "🧭"
        else:
            icon = "🔷"

        tool = await tool_dao.get_by_name(tool_name)
        if not tool:
            tool = await tool_dao.create(
                obj_in={
                    "name": tool_name,
                    "display_name": f"Atlassian: {raw_name}",
                    "description": tool_desc,
                    "type": "mcp",
                    "category": "atlassian",
                    "icon": icon,
                    "parameters_schema": tool_schema,
                    "config": {},
                    "config_schema": {},
                    "mcp_server_url": ATLASSIAN_MCP_URL,
                    "mcp_server_name": "Atlassian Rovo",
                    "mcp_tool_name": raw_name,
                    "enabled": True,
                    "is_default": False,
                    "source": "admin",
                }
            )
        else:
            tool = await tool_dao.update(
                db_obj=tool,
                obj_in={"description": tool_desc, "parameters_schema": tool_schema},
            )

        agent_tool_config: JsonObject = {"api_key": api_key}
        existing = await agent_tool_dao.get_assignment(agent_id, tool.id)
        if existing:
            _ = await agent_tool_dao.update(
                db_obj=existing,
                obj_in={"enabled": True, "config": agent_tool_config},
            )
        else:
            _ = await agent_tool_dao.create(
                obj_in={
                    "agent_id": agent_id,
                    "tool_id": tool.id,
                    "enabled": True,
                    "source": "user_installed",
                    "installed_by_agent_id": agent_id,
                    "config": agent_tool_config,
                }
            )
            assigned += 1

    logger.info(f"[AtlassianChannel] {assigned} new tool assignments for agent {agent_id}")
