"""MCP execution and resource-discovery helpers."""

import uuid

from app.core.logging import logger
from app.services.agent_tool_exec.registry import ToolArguments


async def _execute_mcp_tool(tool_name: str, arguments: ToolArguments, agent_id: uuid.UUID | None = None) -> str:
    """Execute a tool via MCP if it exists in the DB as an MCP tool."""
    from app.dao.tool_dao import agent_tool_dao, tool_dao
    from app.services import agent_tools

    try:
        from app.services.mcp_client import MCPClient

        # Primary lookup: maraclaw-prefixed name (e.g.
        # mcp_shibui_finance_unlock_financial_analysis).
        tool = await tool_dao.get_by_name(tool_name)
        if tool and getattr(tool, "type", "mcp") != "mcp":
            tool = None

        # Fallback: LLM sometimes drops the mcp_<server>_ prefix and calls
        # the bare MCP-side tool name (e.g. unlock_financial_analysis).
        if not tool:
            tool = await tool_dao.get_mcp_by_mcp_tool_name(tool_name)

        if not tool:
            logger.warning(f"[MCP] Unknown tool: {tool_name}")
            return f"Unknown tool: {tool_name}"

        # Load per-agent config override
        agent_config = {}
        if tool and agent_id:
            at = await agent_tool_dao.get_assignment(agent_id, tool.id)
            agent_config = (at.config or {}) if at else {}

        if not tool.mcp_server_url:
            logger.error(f"[MCP] Tool {tool_name} has no server URL configured")
            return f"❌ MCP tool {tool_name} has no server URL configured"

        # Merge global config + agent override
        merged_config = {**(tool.config or {}), **agent_config}
        merged_config = agent_tools._decrypt_sensitive_fields(merged_config)

        mcp_url = tool.mcp_server_url
        mcp_name = tool.mcp_tool_name or tool_name

        # Detect Smithery-hosted MCP servers (*.run.tools URLs)
        # These need Smithery Connect to route tool calls
        if ".run.tools" in mcp_url and merged_config:
            from app.services.agent_tool_exec.mcp_smithery import _execute_via_smithery_connect

            return await _execute_via_smithery_connect(mcp_url, mcp_name, arguments, merged_config, agent_id=agent_id)

        # Direct MCP call for non-Smithery servers
        # Priority for API key:
        # 1. Per-agent tool config (api_key / atlassian_api_key)
        # 2. Agent's Atlassian channel config (for atlassian_* tools)
        direct_api_key_value = merged_config.get("api_key") or merged_config.get("atlassian_api_key")
        direct_api_key = direct_api_key_value if isinstance(direct_api_key_value, str) else None
        if not direct_api_key and tool.mcp_server_name == "Atlassian Rovo" and agent_id is not None:
            try:
                from app.api.atlassian import get_atlassian_api_key_for_agent

                direct_api_key = await get_atlassian_api_key_for_agent(agent_id)
            except Exception:
                _ = None
        client = MCPClient(mcp_url, api_key=direct_api_key)
        return await client.call_tool(mcp_name, arguments)

    except Exception as e:
        logger.exception(f"[MCP] Tool execution error: {tool_name}")
        return f"❌ MCP tool execution error: {str(e)[:200]}"


async def _discover_resources(agent_id: uuid.UUID, arguments: ToolArguments) -> str:
    """Search Smithery registry for MCP servers."""
    query_value = arguments.get("query", "")
    query = query_value if isinstance(query_value, str) else ""
    if not query:
        return "❌ Please provide a search query describing the capability you need."
    max_results_value = arguments.get("max_results", 5)
    max_results = (
        min(max_results_value, 10)
        if isinstance(max_results_value, int) and not isinstance(max_results_value, bool)
        else 5
    )

    from app.services.resource_discovery import search_smithery

    return await search_smithery(query, max_results, agent_id=agent_id)


async def _import_mcp_server(agent_id: uuid.UUID, arguments: ToolArguments) -> str:
    """Import an MCP server — either from Smithery or by direct URL."""
    config_value = arguments.get("config")
    config = config_value if isinstance(config_value, dict) else {}
    reauthorize = arguments.get("reauthorize") is True
    mcp_url_value = config.pop("mcp_url", None)
    mcp_url = mcp_url_value if isinstance(mcp_url_value, str) else None

    if mcp_url:
        # Direct URL import — bypass Smithery
        from app.services.resource_discovery import import_mcp_direct

        server_name_value = arguments.get("server_id") or config.pop("server_name", None)
        server_name = server_name_value if isinstance(server_name_value, str) else None
        api_key_value = config.pop("api_key", None)
        api_key = api_key_value if isinstance(api_key_value, str) else None
        return await import_mcp_direct(mcp_url, agent_id, server_name, api_key)

    # Smithery import
    server_id_value = arguments.get("server_id", "")
    server_id = server_id_value if isinstance(server_id_value, str) else ""
    if not server_id:
        return "❌ Please provide a server_id (e.g. 'github'). Use discover_resources first to find available servers."

    from app.services.resource_discovery import import_mcp_from_smithery

    return await import_mcp_from_smithery(server_id, agent_id, config or None, reauthorize=reauthorize)
