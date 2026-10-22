"""Resource discovery - search Smithery & ModelScope registries and import MCP servers."""

import uuid
from typing import NotRequired, TypedDict

import httpx

from app.core.json_types import (
    JsonObject,
    is_json_object,
    json_as_bool,
    json_as_int,
    json_as_str,
    json_as_str_or,
    json_loads_value,
    json_object_from,
    json_object_from_response,
)
from app.core.logging import logger
from app.dao.agent_dao import agent_dao
from app.dao.tool_dao import agent_tool_dao, tool_dao
from app.services.mcp_installation import MCPInstallation, persist_mcp_installation
from app.services.tool_config import decrypt_sensitive_fields, encrypt_sensitive_fields, get_tenant_tool_config


class ToolParameterSchema(TypedDict):
    type: str
    properties: dict[str, JsonObject]
    required: NotRequired[list[str]]


# ── Smithery Registry Search ────────────────────────────────────

SMITHERY_API_BASE = "https://registry.smithery.ai"
MODELSCOPE_API_BASE = "https://modelscope.cn"


class RegistrySearchResult(TypedDict):
    name: str
    display_name: str
    description: str
    remote: bool
    verified: bool
    use_count: int
    homepage: str
    source: str


class SmitheryConnectionSuccess(TypedDict):
    namespace: str
    connection_id: str
    auth_url: NotRequired[str]


class SmitheryConnectionFailure(TypedDict):
    error: str


class DiscoveredMCPTool(TypedDict):
    name: str
    description: str
    parameters_schema: ToolParameterSchema


def _normalize_discovered_mcp_tool(mcp_tool: JsonObject) -> DiscoveredMCPTool | None:
    name = mcp_tool.get("name")
    if not isinstance(name, str) or not name:
        return None

    description = mcp_tool.get("description")
    raw_schema = mcp_tool.get("inputSchema")
    parameters_schema: ToolParameterSchema = {"type": "object", "properties": {}}
    if is_json_object(raw_schema):
        schema_type = raw_schema.get("type")
        properties = raw_schema.get("properties")
        if isinstance(schema_type, str) and is_json_object(properties):
            normalized_properties: dict[str, JsonObject] = {
                field_name: property_schema
                for field_name, property_schema in properties.items()
                if is_json_object(property_schema)
            }
            if len(normalized_properties) == len(properties):
                parameters_schema = {"type": schema_type, "properties": normalized_properties}
            required = raw_schema.get("required")
            if isinstance(required, list) and all(isinstance(field_name, str) for field_name in required):
                parameters_schema["required"] = [field_name for field_name in required if isinstance(field_name, str)]

    return {
        "name": name,
        "description": description[:500] if isinstance(description, str) else "",
        "parameters_schema": parameters_schema,
    }


async def _get_smithery_api_key(agent_id: uuid.UUID | None = None) -> str:
    """Read Smithery API key.

    Priority: 1) per-agent AgentTool config, 2) system-level tool config.

    Sensitive fields in tool/AgentTool config are stored encrypted (see
    api.tools._encrypt_sensitive_fields). We must decrypt here before
    handing the value to httpx - otherwise Smithery rejects with 401.
    Invalid stored ciphertext leaves this optional credential unavailable.
    """

    def _decrypt(raw: str) -> str:
        if not raw:
            return ""
        decrypted = decrypt_sensitive_fields({"value": raw}, {"fields": [{"key": "value", "type": "password"}]}).get(
            "value"
        )
        return decrypted if isinstance(decrypted, str) else ""

    try:
        agent_tenant_id = None
        if agent_id:
            agent = await agent_dao.get(agent_id)
            agent_tenant_id = agent.tenant_id if agent else None

        if agent_id:
            for at in await agent_tool_dao.list_for_agent(agent_id):
                value: object = at.config.get("smithery_api_key") if at.config else None
                if isinstance(value, str) and value:
                    return _decrypt(value)
        for tool_name in ("discover_resources", "import_mcp_server"):
            tool = await tool_dao.get_by_name(tool_name)
            if not tool:
                continue
            tenant_config = await get_tenant_tool_config(agent_tenant_id, tool.name, tool.config_schema)
            tenant_value: object = tenant_config.get("smithery_api_key")
            if isinstance(tenant_value, str) and tenant_value:
                return tenant_value
            stored: object = tool.config.get("smithery_api_key") if tool.config and not agent_tenant_id else None
            if isinstance(stored, str) and stored:
                return _decrypt(stored)
    except Exception:
        logger.warning("Unable to read Smithery API key configuration")
    return ""


async def _search_smithery_api(query: str, max_results: int, api_key: str) -> list[RegistrySearchResult]:
    """Search Smithery registry, returns normalized results."""
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(
                f"{SMITHERY_API_BASE}/servers",
                params={"q": query, "pageSize": max_results},
                headers=headers,
            )
            if resp.status_code != 200:
                return []
            data = json_object_from_response(resp)
        servers_raw = data.get("servers", [])
        servers = [srv for srv in servers_raw if is_json_object(srv)] if isinstance(servers_raw, list) else []
        return [
            {
                "name": json_as_str_or(srv.get("qualifiedName")),
                "display_name": json_as_str_or(srv.get("displayName")),
                "description": json_as_str_or(srv.get("description"))[:200],
                "remote": json_as_bool(srv.get("remote")),
                "verified": json_as_bool(srv.get("verified")),
                "use_count": json_as_int(srv.get("useCount")),
                "homepage": json_as_str_or(srv.get("homepage")),
                "source": "Smithery",
            }
            for srv in servers[:max_results]
        ]
    except Exception as error:
        logger.warning(f"[ResourceDiscovery] Smithery search failed: {error}")
        return []


async def _get_modelscope_api_token(agent_id: uuid.UUID | None = None) -> str:
    """Read ModelScope API token from discover_resources tool config."""
    try:
        agent_tenant_id = None
        if agent_id:
            agent = await agent_dao.get(agent_id)
            agent_tenant_id = agent.tenant_id if agent else None
        for tool_name in ("discover_resources", "import_mcp_server"):
            tool = await tool_dao.get_by_name(tool_name)
            if not tool:
                continue
            tenant_config = await get_tenant_tool_config(agent_tenant_id, tool.name, tool.config_schema)
            tenant_value: object = tenant_config.get("modelscope_api_token")
            if isinstance(tenant_value, str) and tenant_value:
                return tenant_value
            stored: object = tool.config.get("modelscope_api_token") if tool.config and not agent_tenant_id else None
            if isinstance(stored, str) and stored:
                decrypted = decrypt_sensitive_fields(
                    {"modelscope_api_token": stored},
                    {"fields": [{"key": "modelscope_api_token", "type": "password"}]},
                ).get("modelscope_api_token")
                return decrypted if isinstance(decrypted, str) else ""
    except Exception:
        logger.warning("Unable to read ModelScope API token configuration")
    return ""


async def _search_modelscope_api(
    query: str, max_results: int, agent_id: uuid.UUID | None = None
) -> list[RegistrySearchResult]:
    """Search ModelScope MCP Hub via official OpenAPI (no WAF issues)."""
    api_token = await _get_modelscope_api_token(agent_id)
    if not api_token:
        return []  # Silently skip if no token configured

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_token}",
        "Cookie": f"m_session_id={api_token}",
        "User-Agent": "modelscope-mcp-server/1.0",
    }
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.put(
                f"{MODELSCOPE_API_BASE}/openapi/v1/mcp/servers",
                json={"page_size": max_results, "page_number": 1, "search": query, "filter": {}},
                headers=headers,
            )
            if resp.status_code != 200:
                return []
            data = json_object_from_response(resp)
            if (success := data.get("success")) is None or not success:
                return []

        payload = json_object_from(data.get("data"))
        servers_raw = payload.get("mcp_server_list", [])
        servers_data = [srv for srv in servers_raw if is_json_object(srv)] if isinstance(servers_raw, list) else []
        if not servers_data:
            return []

        results: list[RegistrySearchResult] = []
        for srv in servers_data[:max_results]:
            server_id = json_as_str_or(srv.get("id"))
            results.append(
                {
                    "name": server_id,
                    "display_name": json_as_str_or(srv.get("name"), server_id),
                    "description": json_as_str_or(srv.get("description"))[:200],
                    "remote": json_as_bool(srv.get("is_hosted")),
                    "verified": True,
                    "use_count": 0,
                    "homepage": f"https://modelscope.cn/mcp/servers/{server_id}",
                    "source": "ModelScope",
                }
            )
        return results
    except Exception as e:
        logger.error(f"[ResourceDiscovery] ModelScope search failed: {e}")
        return []


async def search_registries(query: str, max_results: int = 5, agent_id: uuid.UUID | None = None) -> str:
    """Search both Smithery and ModelScope for MCP servers."""
    api_key = await _get_smithery_api_key(agent_id)

    # Search both registries in parallel
    import asyncio

    smithery_task = _search_smithery_api(query, max_results, api_key)
    modelscope_task = _search_modelscope_api(query, max_results, agent_id)
    smithery_results, modelscope_results = await asyncio.gather(smithery_task, modelscope_task)

    # Merge: Smithery first, then ModelScope (deduplicate by name)
    seen_names: set[str] = set()
    all_results: list[RegistrySearchResult] = []
    for r in smithery_results + modelscope_results:
        if r["name"] not in seen_names:
            seen_names.add(r["name"])
            all_results.append(r)

    if not all_results:
        return f'🔍 No MCP servers found for "{query}" on Smithery or ModelScope. Try different keywords.'

    results = []
    for i, srv in enumerate(all_results[:max_results], 1):
        verified = " ✅" if srv["verified"] else ""
        source_tag = f"[{srv['source']}]"
        deploy_info = "🌐 Remote (no local install needed)" if srv["remote"] else "💻 Local install required"
        use_info = f" · 👥 {srv['use_count']:,} users" if srv["use_count"] else ""
        hp = srv["homepage"]

        results.append(
            f"**{i}. {srv['display_name']}**{verified} {source_tag}\n"
            + f"   ID: `{srv['name']}`\n"
            + f"   {srv['description']}\n"
            + f"   {deploy_info}{use_info}\n"
            + f"   {'🔗 ' + hp if hp else ''}"
        )

    header = f'🔍 Found {len(results)} MCP server(s) for "{query}":\n\n'
    footer = (
        "\n\n---\n"
        + "💡 To import a remote server, use `import_mcp_server` with the server ID.\n"
        + '   Example: import_mcp_server(server_id="gmail")'
    )
    return header + "\n\n".join(results) + footer


# ── Import MCP Server ───────────────────────────────────────────


async def _ensure_smithery_connection(
    api_key: str, mcp_url: str, display_name: str
) -> SmitheryConnectionSuccess | SmitheryConnectionFailure:
    """Create or reuse a Smithery Connect namespace + connection.

    Returns dict with keys: namespace, connection_id, auth_url (if OAuth needed).
    """
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            # Get or create namespace
            ns_resp = await client.get("https://api.smithery.ai/namespaces", headers=headers)
            namespaces_payload = json_object_from_response(ns_resp) if ns_resp.status_code == 200 else {}
            namespaces_raw = namespaces_payload.get("namespaces", [])
            namespaces = (
                [item for item in namespaces_raw if is_json_object(item)] if isinstance(namespaces_raw, list) else []
            )
            if namespaces:
                namespace = json_as_str_or(namespaces[0].get("name"))
                if not namespace:
                    return {"error": "Failed to read namespace name"}
            else:
                create_ns = await client.post(
                    "https://api.smithery.ai/namespaces",
                    json={"name": "maraclaw"},
                    headers=headers,
                )
                if create_ns.status_code not in (200, 201):
                    return {"error": f"Failed to create namespace: HTTP {create_ns.status_code}"}
                namespace = json_as_str_or(json_object_from_response(create_ns).get("name"))
                if not namespace:
                    return {"error": f"Failed to create namespace: HTTP {create_ns.status_code}"}

            # Create connection
            conn_id = display_name.lower().replace(" ", "-").replace(":", "")
            conn_resp = await client.post(
                f"https://api.smithery.ai/connect/{namespace}",
                json={"connectionId": conn_id, "mcpUrl": mcp_url, "name": display_name},
                headers=headers,
            )
            if conn_resp.status_code not in (200, 201):
                return {"error": f"Failed to create connection: HTTP {conn_resp.status_code} - {conn_resp.text[:200]}"}

            conn_data = json_object_from_response(conn_resp)
            result: SmitheryConnectionSuccess = {
                "namespace": namespace,
                "connection_id": json_as_str_or(conn_data.get("connectionId"), conn_id),
            }
            status = conn_data.get("status", {})
            if is_json_object(status) and status.get("state") == "auth_required":
                result["auth_url"] = json_as_str_or(status.get("authorizationUrl"))
            return result
    except Exception as e:
        return {"error": str(e)[:200]}


async def import_mcp_from_smithery(
    server_id: str,
    agent_id: uuid.UUID,
    config: JsonObject | None = None,
    reauthorize: bool = False,
) -> str:
    """Import an MCP server from Smithery into the platform.

    Uses the Smithery Registry detail API to get tool definitions,
    and stores the deploymentUrl for runtime execution via Smithery Connect.
    If config contains 'smithery_api_key', it's stored per-agent for future use.
    """
    server_config: JsonObject = dict(config) if config is not None and config else {}  # mutable copy

    # Extract smithery_api_key from config (user-provided) or fallback to stored
    configured_api_key = server_config.pop("smithery_api_key", None)
    api_key = (
        configured_api_key
        if isinstance(configured_api_key, str) and configured_api_key
        else await _get_smithery_api_key(agent_id)
    )
    if not api_key:
        return (
            "❌ Smithery API key is required to import MCP servers.\n\n"
            + "Provide your Smithery API key. You can obtain one by:\n"
            + "1. Signing up for or logging in to https://smithery.ai\n"
            + "2. Creating an API key at https://smithery.ai/account/api-keys\n"
            + "3. Providing the key, for example:\n"
            + '   `import_mcp_server(server_id="github", config={"smithery_api_key": "your-key"})`'
        )

    # Write key back to discover_resources / import_mcp_server AgentTool configs
    # so it shows up in the Config dialog
    try:
        for tool_name in ("discover_resources", "import_mcp_server"):
            tool = await tool_dao.get_by_name(tool_name)
            if not tool:
                continue
            _ = await agent_tool_dao.ensure_with_config(
                agent_id,
                tool.id,
                config=encrypt_sensitive_fields(
                    {"smithery_api_key": api_key},
                    {"fields": [{"key": "smithery_api_key", "type": "password"}]},
                ),
                source="system",
            )
    except Exception:
        logger.warning("Unable to persist Smithery API key configuration")

    # Step 1: Search for server by ID
    headers = {"Accept": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(
                f"{SMITHERY_API_BASE}/servers",
                params={"q": server_id.lstrip("@"), "pageSize": 5},
                headers=headers,
            )
            if resp.status_code != 200:
                return f"❌ Server '{server_id}' not found on Smithery (HTTP {resp.status_code})"
            data = json_object_from_response(resp)
            servers_raw = data.get("servers", [])
            servers = [item for item in servers_raw if is_json_object(item)] if isinstance(servers_raw, list) else []
            server_info: JsonObject | None = None
            clean_id = server_id.lstrip("@")
            for s in servers:
                qualified = s.get("qualifiedName")
                if qualified in (clean_id, server_id):
                    server_info = s
                    break
            if (server_info is None or not server_info) and servers:
                server_info = servers[0]
            if server_info is None or not server_info:
                return f"❌ Server '{server_id}' not found on Smithery."
    except Exception as e:
        return f"❌ Failed to fetch server info: {str(e)[:200]}"

    display_name = json_as_str_or(server_info.get("displayName"), server_id.split("/")[-1])
    description = json_as_str_or(server_info.get("description"))
    qualified_name = json_as_str_or(server_info.get("qualifiedName"), server_id.lstrip("@"))

    # Check if server supports remote hosting
    if (remote := server_info.get("remote")) is None or not remote:
        return (
            f"⚠️ **{display_name}** (`{qualified_name}`) does not support remote hosting via Smithery Connect.\n"
            + "This server requires local installation and cannot be imported automatically.\n"
            + f"🔗 {json_as_str_or(server_info.get('homepage'))}"
        )

    # Step 2: Get full server details including tools from registry API
    tools_discovered: list[JsonObject] = []
    deployment_url = None
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            detail_resp = await client.get(
                f"{SMITHERY_API_BASE}/servers/{qualified_name}",
                headers=headers,
            )
            if detail_resp.status_code == 200:
                detail = json_object_from_response(detail_resp)
                deployment_url = json_as_str(detail.get("deploymentUrl"))
                raw_tools = detail.get("tools", [])
                tools_discovered = [
                    {
                        "name": json_as_str_or(t.get("name")),
                        "description": json_as_str_or(t.get("description")),
                        "inputSchema": json_object_from(t.get("inputSchema")),
                    }
                    for t in (raw_tools if isinstance(raw_tools, list) else [])
                    if is_json_object(t) and (name := t.get("name")) is not None and name
                ]
                logger.info(f"[ResourceDiscovery] Got {len(tools_discovered)} tools from registry for {qualified_name}")
            else:
                logger.warning(
                    f"[ResourceDiscovery] Could not fetch detail for {qualified_name}: HTTP {detail_resp.status_code}"
                )
    except Exception as e:
        logger.error(f"[ResourceDiscovery] Could not fetch server detail: {e}")

    # Step 3: Determine the MCP server URL for runtime execution
    base_mcp_url = deployment_url or f"https://{qualified_name}.run.tools"

    # Step 3.5: Auto-create Smithery Connect namespace + connection
    smithery_config: JsonObject = {}  # will be merged into every AgentTool.config
    auth_message = ""
    conn_result = await _ensure_smithery_connection(api_key, base_mcp_url, display_name)
    conn_error = json_as_str(conn_result.get("error"))
    if conn_error not in (None, ""):
        auth_message = f"\n\n⚠️ Could not auto-create Smithery connection: {conn_error}"
    else:
        smithery_config = {
            "smithery_namespace": json_as_str_or(conn_result.get("namespace")),
            "smithery_connection_id": json_as_str_or(conn_result.get("connection_id")),
        }
        auth_url = json_as_str(conn_result.get("auth_url"))
        if auth_url not in (None, ""):
            auth_message = (
                "\n\n🔐 **OAuth authorization required**: Open the following link in a browser to complete authorization:\n"
                + f"{auth_url}\n"
                + "The tool will be available after authorization is complete."
            )

    # Step 3.6: Override registry-advertised schema with the runtime server's
    # actual tools/list. Smithery's registry detail can drift behind the live
    # server (we hit this with shibui/finance: registry said `sql`, server
    # required `user_prompt` + `query`). The truth is whatever tools/list
    # returns at call time, so prefer it whenever available.
    live_ready = False
    if smithery_config:
        ns_ = json_as_str_or(smithery_config["smithery_namespace"])
        conn_ = json_as_str_or(smithery_config["smithery_connection_id"])
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                live_resp = await client.post(
                    f"https://api.smithery.ai/connect/{ns_}/{conn_}/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream",
                    },
                )
            if live_resp.status_code == 200:
                live_data: object = None
                # Smithery Connect returns SSE; parse the first data: line.
                for line in live_resp.text.split("\n"):
                    line = line.strip()
                    if line.startswith("data: "):
                        try:
                            live_data = json_loads_value(line[6:])
                            break
                        except ValueError:
                            pass
                if live_data is None:
                    try:
                        live_data = json_loads_value(live_resp.text)
                    except ValueError:
                        live_data = None
                live_payload = live_data if is_json_object(live_data) else None
                live_result = (
                    json_object_from(live_payload.get("result")) if live_payload is not None and live_payload else {}
                )
                live_tools_raw = live_result.get("tools", [])
                live_tools = (
                    [item for item in live_tools_raw if is_json_object(item)]
                    if isinstance(live_tools_raw, list)
                    else []
                )
                # MCP servers also return prompts here; only treat actual tools.
                live_tools_normalized: list[JsonObject] = [
                    {
                        "name": json_as_str_or(t.get("name")),
                        "description": json_as_str_or(t.get("description")),
                        "inputSchema": json_object_from(t.get("inputSchema")),
                    }
                    for t in live_tools
                    if (name := t.get("name")) is not None and name and is_json_object(t.get("inputSchema"))
                ]
                if live_tools_normalized:
                    logger.info(
                        f"[ResourceDiscovery] Using live tools/list for {qualified_name}: "
                        + f"{len(live_tools_normalized)} tool(s) override registry's "
                        + f"{len(tools_discovered)}"
                    )
                    tools_discovered = live_tools_normalized
                    live_ready = not auth_message
        except Exception as e:
            logger.warning(
                f"[ResourceDiscovery] Live tools/list failed for {qualified_name}, falling back to registry schema: {e}"
            )

    normalized_tools = [tool for raw in tools_discovered if (tool := _normalize_discovered_mcp_tool(raw)) is not None]
    imported_tools = await persist_mcp_installation(
        agent_id,
        MCPInstallation(
            prefix=f"mcp_{server_id.replace('/', '_').replace('@', '')}",
            display_name=display_name,
            server_url=base_mcp_url,
            description=description or f"MCP Server: {server_id}",
            config={**smithery_config, **server_config},
        ),
        normalized_tools,
    )

    result = f"🔌 Imported MCP server: **{display_name}** (`{server_id}`)\n\n"
    result += "\n".join(imported_tools)
    result += f"\n\n📡 MCP Server URL: `{base_mcp_url}`"
    if auth_message:
        result += auth_message
    elif live_ready and normalized_tools:
        result += "\n\n💡 The imported tools are now available for use."
    else:
        result += "\n\n⚠️ Configuration saved, but the server is not ready: live tools/list was not verified. Retry after configuring or authorizing the server."
    return result


# ── Direct URL Import ───────────────────────────────────────────


async def import_mcp_direct(
    mcp_url: str,
    agent_id: uuid.UUID,
    server_name: str | None = None,
    api_key: str | None = None,
) -> str:
    """Import an MCP server by directly connecting to its HTTP/SSE endpoint.

    This bypasses Smithery entirely - useful for self-hosted or third-party
    MCP servers that provide their own public endpoint.
    """
    from app.core.outbound_policy import OutboundPolicyError, validate_public_url
    from app.services.mcp_client import MCPClient

    try:
        _ = await validate_public_url(mcp_url)
    except OutboundPolicyError:
        return "MCP import blocked: server destination is not allowed."

    client = MCPClient(mcp_url)
    if api_key not in (None, ""):
        client.api_key = api_key

    display_name = server_name or mcp_url.split("//")[-1].split("/")[0].split(":")[0]
    safe_name = display_name.replace(".", "_").replace("/", "_").replace(":", "_").replace("-", "_")

    # Try to list tools from the endpoint
    tools_discovered: list[JsonObject] = []
    try:
        tools_discovered = await client.list_tools()
        logger.info("[DirectImport] Got {} tools", len(tools_discovered))
    except OutboundPolicyError:
        return "MCP import blocked: server destination is not allowed."
    except Exception:
        logger.error("[DirectImport] Could not list tools")

    normalized_tools = [tool for raw in tools_discovered if (tool := _normalize_discovered_mcp_tool(raw)) is not None]
    imported_tools = await persist_mcp_installation(
        agent_id,
        MCPInstallation(
            prefix=f"mcp_{safe_name}",
            display_name=display_name,
            server_url=client.server_url,
            description=f"MCP Server: {client.server_url}",
            config={"api_key": client.api_key} if client.api_key not in (None, "") else {},
        ),
        normalized_tools,
    )

    result = f"🔌 Imported MCP server: **{display_name}**\n\n"
    result += "\n".join(imported_tools)
    result += f"\n\n📡 MCP Server URL: `{client.server_url}`"
    if normalized_tools:
        result += "\n\n💡 The imported tools are now available for use."
    else:
        result += "\n\n⚠️ Configuration saved, but the server is not ready: tools/list returned no usable tools. Retry after configuring or authorizing the server."
    return result


# ── Atlassian Rovo MCP Auto-Seeding ─────────────────────────────────────────

ATLASSIAN_ROVO_MCP_URL = "https://mcp.atlassian.com/v1/mcp"
ATLASSIAN_ROVO_SERVER_NAME = "Atlassian Rovo"
ATLASSIAN_ROVO_TOOL_PREFIX = "atlassian_rovo_"


async def seed_atlassian_rovo_tools(api_key: str) -> None:
    """Connect to Atlassian Rovo MCP and seed all available tools as platform-level MCP tools.

    Called on startup when an API key is configured. Existing tools are updated in-place;
    new tools discovered from the server are created. The api_key is stored in each tool's
    config so _execute_mcp_tool can authenticate requests.
    """
    from app.services.mcp_client import MCPClient

    logger.info(f"[AtlassianRovo] Connecting to {ATLASSIAN_ROVO_MCP_URL} ...")
    try:
        client = MCPClient(ATLASSIAN_ROVO_MCP_URL, api_key=api_key)
        tools_discovered = await client.list_tools()
    except Exception as e:
        logger.error(f"[AtlassianRovo] Could not list tools: {e}")
        return

    if not tools_discovered:
        logger.warning("[AtlassianRovo] No tools returned from server")
        return

    logger.info(f"[AtlassianRovo] Discovered {len(tools_discovered)} tools")

    upserted = 0
    for raw_mcp_tool in tools_discovered:
        mcp_tool = _normalize_discovered_mcp_tool(raw_mcp_tool)
        if mcp_tool is None:
            continue
        raw_name = mcp_tool["name"]

        tool_name = f"{ATLASSIAN_ROVO_TOOL_PREFIX}{raw_name}"
        tool_display = f"Atlassian: {raw_name}"
        tool_desc = mcp_tool["description"]
        tool_schema = mcp_tool["parameters_schema"]

        if "jira" in raw_name.lower() or "issue" in raw_name.lower():
            icon = "🔵"
        elif "confluence" in raw_name.lower() or "page" in raw_name.lower():
            icon = "📘"
        elif "compass" in raw_name.lower() or "component" in raw_name.lower():
            icon = "🧭"
        else:
            icon = "🔷"

        existing_tool = await tool_dao.get_by_name(tool_name)
        atlassian_config: JsonObject = {"api_key": api_key}

        if existing_tool:
            _ = await tool_dao.update(
                db_obj=existing_tool,
                obj_in={
                    "description": tool_desc,
                    "parameters_schema": tool_schema,
                    "config": atlassian_config,
                },
            )
        else:
            _ = await tool_dao.create(
                obj_in={
                    "name": tool_name,
                    "display_name": tool_display,
                    "description": tool_desc,
                    "type": "mcp",
                    "category": "atlassian",
                    "icon": icon,
                    "parameters_schema": tool_schema,
                    "mcp_server_url": ATLASSIAN_ROVO_MCP_URL,
                    "mcp_server_name": ATLASSIAN_ROVO_SERVER_NAME,
                    "mcp_tool_name": raw_name,
                    "enabled": True,
                    "is_default": False,
                    "config": atlassian_config,
                    "source": "admin",
                }
            )
            upserted += 1

    logger.info(f"[AtlassianRovo] Seeded {upserted} new Atlassian Rovo tools")


async def refresh_atlassian_rovo_api_key(api_key: str) -> None:
    """Update the stored api_key in all Atlassian Rovo tool records.

    Called when the user updates the API key via the config UI.
    """
    _ = await tool_dao.update_config_for_mcp_server(ATLASSIAN_ROVO_SERVER_NAME, {"api_key": api_key})
    logger.info("[AtlassianRovo] API key refreshed for all Rovo tools")
