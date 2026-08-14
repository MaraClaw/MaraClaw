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
    json_object_from,
)
from app.core.logging import logger
from app.dao.agent_dao import agent_dao
from app.dao.tool_dao import agent_tool_dao, tool_dao
from app.services.tool_config import decrypt_sensitive_fields, get_tenant_tool_config


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
    if isinstance(raw_schema, dict):
        schema_type = raw_schema.get("type")
        properties = raw_schema.get("properties")
        if isinstance(schema_type, str) and isinstance(properties, dict):
            normalized_properties: dict[str, JsonObject] = {
                field_name: property_schema
                for field_name, property_schema in properties.items()
                if isinstance(property_schema, dict)
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
    Falls back to raw value when decrypt fails (e.g. legacy plaintext keys).
    """

    def _maybe_decrypt(raw: str) -> str:
        if not raw:
            return ""
        decrypted = decrypt_sensitive_fields({"value": raw}, {"fields": [{"key": "value", "type": "password"}]}).get(
            "value", raw
        )
        return decrypted if isinstance(decrypted, str) else raw

    try:
        agent_tenant_id = None
        if agent_id:
            agent = await agent_dao.get(agent_id)
            agent_tenant_id = agent.tenant_id if agent else None

        if agent_id:
            for at in await agent_tool_dao.list_for_agent(agent_id):
                value = at.config.get("smithery_api_key") if at.config else None
                if isinstance(value, str) and value:
                    return _maybe_decrypt(value)
        for tool_name in ("discover_resources", "import_mcp_server"):
            tool = await tool_dao.get_by_name(tool_name)
            if not tool:
                continue
            tenant_config = await get_tenant_tool_config(None, agent_tenant_id, tool.name, tool.config_schema)
            tenant_value = tenant_config.get("smithery_api_key")
            if isinstance(tenant_value, str) and tenant_value:
                return tenant_value
            value = tool.config.get("smithery_api_key") if tool.config and not agent_tenant_id else None
            if isinstance(value, str) and value:
                return _maybe_decrypt(value)
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
            data = json_object_from(resp.json())
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
            tenant_config = await get_tenant_tool_config(None, agent_tenant_id, tool.name, tool.config_schema)
            tenant_value = tenant_config.get("modelscope_api_token")
            if isinstance(tenant_value, str) and tenant_value:
                return tenant_value
            value = tool.config.get("modelscope_api_token") if tool.config and not agent_tenant_id else None
            if isinstance(value, str) and value:
                return value
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
            data = json_object_from(resp.json())
            if not data.get("success"):
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


# Keep backward-compatible alias
async def search_smithery(query: str, max_results: int = 5, agent_id: uuid.UUID | None = None) -> str:
    return await search_registries(query, max_results, agent_id=agent_id)


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
            namespaces_payload = json_object_from(ns_resp.json()) if ns_resp.status_code == 200 else {}
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
                namespace = json_as_str_or(json_object_from(create_ns.json()).get("name"))
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

            conn_data = json_object_from(conn_resp.json())
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
    server_config: JsonObject = dict(config) if config else {}  # mutable copy

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
                config={"smithery_api_key": api_key},
                source="system",
            )
    except Exception:
        logger.warning("Unable to persist Smithery API key configuration")

    # ---- Early exit: check if this server's tools are already installed for this agent ----
    # Check by both tool name prefix AND mcp_server_name to catch different server_id variants
    # (e.g., "github" vs "@anthropic/github" both produce server_name "GitHub")
    clean_id_check = server_id.replace("/", "_").replace("@", "")
    try:
        prefixes = [f"mcp_{clean_id_check}", f"mcp_{clean_id_check.split('_')[-1]}"]
        existing_server_tools = await tool_dao.list_mcp_by_name_prefixes(prefixes)
        if existing_server_tools and not server_config and not reauthorize:
            tool_ids = [t.id for t in existing_server_tools]
            agent_assignments = []
            for tid in tool_ids:
                at = await agent_tool_dao.get_assignment(agent_id, tid)
                if at:
                    agent_assignments.append(at)
            if len(agent_assignments) >= len(existing_server_tools):
                tool_names = [t.display_name for t in existing_server_tools[:5]]
                more = f" ... and {len(existing_server_tools) - 5} more" if len(existing_server_tools) > 5 else ""
                return (
                    f"⏭️ You already have **{len(existing_server_tools)}** tools from this MCP server installed:\n"
                    + "\n".join(f"  • {n}" for n in tool_names)
                    + more
                    + "\n\nNo action needed. These tools are ready to use."
                    + '\n\n💡 If tools stopped working (e.g. OAuth expired), use `import_mcp_server(server_id="....", reauthorize=true)` to re-authorize.'
                )
    except Exception:
        logger.warning("Unable to check existing Smithery server tools; continuing import")

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
            data = json_object_from(resp.json())
            servers_raw = data.get("servers", [])
            servers = [item for item in servers_raw if is_json_object(item)] if isinstance(servers_raw, list) else []
            server_info: JsonObject | None = None
            clean_id = server_id.lstrip("@")
            for s in servers:
                qualified = s.get("qualifiedName")
                if qualified == clean_id or qualified == server_id:
                    server_info = s
                    break
            if not server_info and servers:
                server_info = servers[0]
            if not server_info:
                return f"❌ Server '{server_id}' not found on Smithery."
    except Exception as e:
        return f"❌ Failed to fetch server info: {str(e)[:200]}"

    display_name = json_as_str_or(server_info.get("displayName"), server_id.split("/")[-1])
    description = json_as_str_or(server_info.get("description"))
    qualified_name = json_as_str_or(server_info.get("qualifiedName"), server_id.lstrip("@"))

    # Check if server supports remote hosting
    if not server_info.get("remote"):
        return (
            f"⚠️ **{display_name}** (`{qualified_name}`) does not support remote hosting via Smithery Connect.\n"
            + f"This server requires local installation and cannot be imported automatically.\n"
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
                detail = json_object_from(detail_resp.json())
                deployment_url = json_as_str(detail.get("deploymentUrl"))
                raw_tools = detail.get("tools", [])
                tools_discovered = [
                    {
                        "name": json_as_str_or(t.get("name")),
                        "description": json_as_str_or(t.get("description")),
                        "inputSchema": json_object_from(t.get("inputSchema")),
                    }
                    for t in (raw_tools if isinstance(raw_tools, list) else [])
                    if is_json_object(t) and t.get("name")
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
    if "error" in conn_result:
        auth_message = f"\n\n⚠️ Could not auto-create Smithery connection: {conn_result['error']}"
    else:
        smithery_config = {
            "smithery_namespace": conn_result["namespace"],
            "smithery_connection_id": conn_result["connection_id"],
        }
        auth_url = json_as_str(conn_result.get("auth_url"))
        if auth_url:
            auth_message = (
                f"\n\n🔐 **OAuth authorization required**: Open the following link in a browser to complete authorization:\n"
                + f"{auth_url}\n"
                + f"The tool will be available after authorization is complete."
            )

    # Step 3.6: Override registry-advertised schema with the runtime server's
    # actual tools/list. Smithery's registry detail can drift behind the live
    # server (we hit this with shibui/finance: registry said `sql`, server
    # required `user_prompt` + `query`). The truth is whatever tools/list
    # returns at call time, so prefer it whenever available.
    if smithery_config:
        ns_ = json_as_str_or(smithery_config["smithery_namespace"])
        conn_ = json_as_str_or(smithery_config["smithery_connection_id"])
        try:
            import json as _json

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
                            live_data = _json.loads(line[6:])
                            break
                        except _json.JSONDecodeError:
                            pass
                if live_data is None:
                    try:
                        live_data = _json.loads(live_resp.text)
                    except _json.JSONDecodeError:
                        live_data = None
                live_payload = live_data if is_json_object(live_data) else None
                live_result = json_object_from(live_payload.get("result")) if live_payload else {}
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
                    if t.get("name") and is_json_object(t.get("inputSchema"))
                ]
                if live_tools_normalized:
                    logger.info(
                        f"[ResourceDiscovery] Using live tools/list for {qualified_name}: "
                        + f"{len(live_tools_normalized)} tool(s) override registry's "
                        + f"{len(tools_discovered)}"
                    )
                    tools_discovered = live_tools_normalized
        except Exception as e:
            logger.warning(
                f"[ResourceDiscovery] Live tools/list failed for {qualified_name}, falling back to registry schema: {e}"
            )

    # Merge smithery_config + user config for AgentTool
    agent_tool_config: JsonObject = {**smithery_config, **server_config}

    imported_tools = []

    async def _ensure_agent_tool(tool_id: uuid.UUID):
        _ = await agent_tool_dao.ensure_with_config(
            agent_id,
            tool_id,
            config=agent_tool_config,
            source="user_installed",
            installed_by_agent_id=agent_id,
        )

    if server_config or reauthorize:
        for et in await tool_dao.list_mcp_by_server_display_name(display_name):
            _ = await tool_dao.update_mcp_server_url(et.id, base_mcp_url)
            await _ensure_agent_tool(et.id)

    if tools_discovered:
        generic_name = f"mcp_{server_id.replace('/', '_').replace('@', '')}"
        old_generic = await tool_dao.get_by_name(generic_name)
        if old_generic:
            await agent_tool_dao.delete_for_tool(old_generic.id)
            _ = await tool_dao.delete(id=old_generic.id)

        for mcp_tool in tools_discovered:
            mcp_tool_name = json_as_str_or(mcp_tool.get("name"))
            if not mcp_tool_name:
                continue
            tool_name = f"mcp_{server_id.replace('/', '_').replace('@', '')}_{mcp_tool_name}"
            tool_display = f"{display_name}: {mcp_tool_name}"

            existing_tool = await tool_dao.get_by_name(tool_name)
            if existing_tool:
                _ = await tool_dao.update_mcp_server_url(existing_tool.id, base_mcp_url)
                await _ensure_agent_tool(existing_tool.id)
                if reauthorize:
                    imported_tools.append(f"🔄 {tool_display} (reauthorized)")
                elif server_config:
                    imported_tools.append(f"🔄 {tool_display} (config updated)")
                else:
                    imported_tools.append(f"⏭️ {tool_display} (already imported)")
                continue

            tool = await tool_dao.create(
                obj_in={
                    "name": tool_name,
                    "display_name": tool_display,
                    "description": (json_as_str_or(mcp_tool.get("description"), description) or "")[:500],
                    "type": "mcp",
                    "category": "mcp",
                    "icon": "🔌",
                    "parameters_schema": json_object_from(
                        mcp_tool.get("inputSchema", {"type": "object", "properties": {}})
                    ),
                    "mcp_server_url": base_mcp_url,
                    "mcp_server_name": display_name,
                    "mcp_tool_name": mcp_tool_name,
                    "enabled": True,
                    "is_default": False,
                    "source": "agent",
                }
            )
            await _ensure_agent_tool(tool.id)
            imported_tools.append(f"✅ {tool_display}")
    else:
        tool_name = f"mcp_{server_id.replace('/', '_').replace('@', '')}"
        tool_display = display_name

        existing_tool = await tool_dao.get_by_name(tool_name)
        if existing_tool:
            _ = await tool_dao.update_mcp_server_url(existing_tool.id, base_mcp_url)
            await _ensure_agent_tool(existing_tool.id)
            if server_config:
                return f"🔄 {tool_display} config updated. The tool is now ready to use."
            return f"⏭️ {tool_display} is already imported."

        tool = await tool_dao.create(
            obj_in={
                "name": tool_name,
                "display_name": tool_display,
                "description": description[:500] or f"MCP Server: {server_id}",
                "type": "mcp",
                "category": "mcp",
                "icon": "🔌",
                "parameters_schema": {"type": "object", "properties": {}},
                "mcp_server_url": base_mcp_url,
                "mcp_server_name": display_name,
                "enabled": True,
                "is_default": False,
                "source": "agent",
            }
        )
        await _ensure_agent_tool(tool.id)
        imported_tools.append(f"✅ {tool_display} (tool list not available from registry - may need configuration)")

    result = f"🔌 Imported MCP server: **{display_name}** (`{server_id}`)\n\n"
    result += "\n".join(imported_tools)
    result += f"\n\n📡 MCP Server URL: `{base_mcp_url}`"
    if auth_message:
        result += auth_message
    else:
        result += "\n\n💡 The imported tools are now available for use."
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
    from app.services.mcp_client import MCPClient

    # Build URL with apiKey if provided
    full_url = mcp_url
    if api_key and "?" in mcp_url:
        full_url = f"{mcp_url}&apiKey={api_key}"
    elif api_key:
        full_url = f"{mcp_url}?apiKey={api_key}"

    display_name = server_name or mcp_url.split("//")[-1].split("/")[0].split(":")[0]
    safe_name = display_name.replace(".", "_").replace("/", "_").replace(":", "_").replace("-", "_")

    # Try to list tools from the endpoint
    tools_discovered: list[JsonObject] = []
    try:
        client = MCPClient(full_url)
        tools_discovered = await client.list_tools()
        logger.info(f"[DirectImport] Got {len(tools_discovered)} tools from {mcp_url}")
    except Exception as e:
        logger.error(f"[DirectImport] Could not list tools from {mcp_url}: {e}")

    # Config to store in AgentTool
    agent_tool_config = {}
    if api_key:
        agent_tool_config["api_key"] = api_key

    imported_tools = []

    async def _ensure_agent_tool(tool_id: uuid.UUID):
        _ = await agent_tool_dao.ensure_with_config(
            agent_id,
            tool_id,
            config=agent_tool_config,
            source="user_installed",
            installed_by_agent_id=agent_id,
        )

    if tools_discovered:
        for raw_mcp_tool in tools_discovered:
            mcp_tool = _normalize_discovered_mcp_tool(raw_mcp_tool)
            if mcp_tool is None:
                continue
            tool_name = f"mcp_{safe_name}_{mcp_tool['name']}"
            tool_display = f"{display_name}: {mcp_tool['name']}"

            existing_tool = await tool_dao.get_by_name(tool_name)
            if existing_tool:
                _ = await tool_dao.update_mcp_server_url(existing_tool.id, mcp_url)
                await _ensure_agent_tool(existing_tool.id)
                imported_tools.append(f"⏭️ {tool_display} (already imported)")
                continue

            tool = await tool_dao.create(
                obj_in={
                    "name": tool_name,
                    "display_name": tool_display,
                    "description": mcp_tool["description"],
                    "type": "mcp",
                    "category": "mcp",
                    "icon": "🔌",
                    "parameters_schema": mcp_tool["parameters_schema"],
                    "mcp_server_url": mcp_url,
                    "mcp_server_name": display_name,
                    "mcp_tool_name": mcp_tool["name"],
                    "enabled": True,
                    "is_default": False,
                    "source": "agent",
                }
            )
            await _ensure_agent_tool(tool.id)
            imported_tools.append(f"✅ {tool_display}")
    else:
        tool_name = f"mcp_{safe_name}"
        existing_tool = await tool_dao.get_by_name(tool_name)
        if existing_tool:
            _ = await tool_dao.update_mcp_server_url(existing_tool.id, mcp_url)
            await _ensure_agent_tool(existing_tool.id)
            return f"⏭️ {display_name} is already imported."

        tool = await tool_dao.create(
            obj_in={
                "name": tool_name,
                "display_name": display_name,
                "description": f"MCP Server: {mcp_url}",
                "type": "mcp",
                "category": "mcp",
                "icon": "🔌",
                "parameters_schema": {"type": "object", "properties": {}},
                "mcp_server_url": mcp_url,
                "mcp_server_name": display_name,
                "enabled": True,
                "is_default": False,
                "source": "agent",
            }
        )
        await _ensure_agent_tool(tool.id)
        imported_tools.append(f"✅ {display_name} (tools couldn't be listed - server may need configuration)")

    result = f"🔌 Imported MCP server: **{display_name}**\n\n"
    result += "\n".join(imported_tools)
    result += f"\n\n📡 MCP Server URL: `{mcp_url}`"
    result += "\n\n💡 The imported tools are now available for use."
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
