from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

import httpx

from app.core.json_types import JsonObject, json_as_str_or
from app.core.logging import logger
from app.db.errors import DbError
from app.records.agent_setup_mcp import MCPWarningCode
from app.services.mcp_installation import MCPInstallation, persist_mcp_installation
from app.services.smithery_tools import discover_server, live_tools
from app.services.tool_config import encrypt_sensitive_fields

if TYPE_CHECKING:
    from app.services.resource_discovery import DiscoveredMCPTool


@dataclass(frozen=True, slots=True)
class MCPImportOutcome:
    warning_code: MCPWarningCode | None
    text: str
    installation: MCPInstallation | None = None
    tools: tuple[DiscoveredMCPTool, ...] = ()


async def _saved_config(agent_id: UUID, prefix: str) -> JsonObject:
    from app.services import resource_discovery as discovery

    for tool in sorted(await discovery.tool_dao.list_mcp_by_name_prefixes([prefix]), key=lambda item: item.name):
        expected_name = f"{prefix}_{tool.mcp_tool_name}" if tool.mcp_tool_name is not None else prefix
        if tool.name != expected_name:
            continue
        assignment = await discovery.agent_tool_dao.get_assignment(agent_id, tool.id)
        if assignment is not None and assignment.config:
            config = assignment.config
            if json_as_str_or(config.get("smithery_namespace")) and json_as_str_or(
                config.get("smithery_connection_id")
            ):
                return dict(config)
    return {}


async def prepare_smithery_import(
    server_id: str, agent_id: UUID, config: JsonObject | None = None, *, reauthorize: bool = False
) -> MCPImportOutcome:
    from app.services import resource_discovery as discovery

    prefix = f"mcp_{server_id.replace('/', '_').replace('@', '')}"
    server_config = {**await _saved_config(agent_id, prefix), **(config or {})}
    configured_key = server_config.pop("smithery_api_key", None)
    api_key = (
        configured_key
        if isinstance(configured_key, str) and configured_key
        else await discovery._get_smithery_api_key(agent_id)
    )
    if not api_key:
        return MCPImportOutcome("missing_credentials", "❌ Smithery API key is required to import MCP servers.")
    if isinstance(configured_key, str) and configured_key:
        try:
            for name in ("discover_resources", "import_mcp_server"):
                tool = await discovery.tool_dao.get_by_name(name)
                if tool is not None:
                    _ = await discovery.agent_tool_dao.ensure_with_config(
                        agent_id,
                        tool.id,
                        config=encrypt_sensitive_fields(
                            {"smithery_api_key": api_key},
                            {"fields": [{"key": "smithery_api_key", "type": "password"}]},
                        ),
                        source="system",
                    )
        except DbError:
            logger.warning("Unable to persist Smithery API key configuration")
    try:
        server = await discover_server(server_id)
        if server is None:
            return MCPImportOutcome("import_failed", f"❌ Server '{server_id}' not found on Smithery.")
        if not server.remote:
            return MCPImportOutcome(
                "unsupported_remote_transport",
                f"⚠️ **{server.display_name}** does not support remote hosting via Smithery Connect.",
            )
        warning: MCPWarningCode | None = None
        auth_message = ""
        connection: JsonObject = {}
        namespace = json_as_str_or(server_config.get("smithery_namespace"))
        connection_id = json_as_str_or(server_config.get("smithery_connection_id"))
        if not reauthorize and namespace and connection_id:
            connection = {
                "smithery_namespace": namespace,
                "smithery_connection_id": connection_id,
            }
        else:
            result = await discovery._ensure_smithery_connection(api_key, server.url, server.display_name)
            if "error" in result:
                warning = "connection_failed"
            else:
                connection = {
                    "smithery_namespace": result["namespace"],
                    "smithery_connection_id": result["connection_id"],
                }
                auth_url = result.get("auth_url")
                if auth_url is not None and auth_url:
                    warning = "authorization_required"
                    auth_message = f"\n\n🔐 **OAuth authorization required**: {auth_url}"
        tools = server.tools
        if connection:
            discovered, live_warning = await live_tools(api_key, connection)
            if discovered:
                tools = discovered
            warning = warning or live_warning
        normalized = tuple(
            parsed for raw in tools if (parsed := discovery._normalize_discovered_mcp_tool(raw)) is not None
        )
        if warning is None and not normalized:
            warning = "tools_unverified"
        installation = MCPInstallation(
            prefix,
            server.display_name,
            server.url,
            server.description or f"MCP Server: {server_id}",
            {**server_config, **connection},
        )
        text = f"🔌 Imported MCP server: **{server.display_name}** (`{server_id}`)\n\n📡 MCP Server URL: `{server.url}`"
        if auth_message:
            text += auth_message
        elif warning is not None:
            text += "\n\n⚠️ Configuration saved, but the server is not ready. Retry after configuring or authorizing the server."
        else:
            text += "\n\n💡 The imported tools are now available for use."
        return MCPImportOutcome(warning, text, installation, normalized)
    except httpx.HTTPError, ValueError:
        return MCPImportOutcome("import_failed", "❌ Failed to import MCP server.")


async def publish_smithery_import(agent_id: UUID, outcome: MCPImportOutcome) -> None:
    if outcome.installation is not None:
        _ = await persist_mcp_installation(agent_id, outcome.installation, list(outcome.tools))


async def import_mcp_from_smithery_result(
    server_id: str, agent_id: UUID, config: JsonObject | None = None, *, reauthorize: bool = False
) -> MCPImportOutcome:
    outcome = await prepare_smithery_import(server_id, agent_id, config, reauthorize=reauthorize)
    await publish_smithery_import(agent_id, outcome)
    return outcome
