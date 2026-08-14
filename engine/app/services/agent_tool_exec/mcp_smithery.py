"""Smithery Connect execution and recovery helpers."""

import uuid

from app.core.json_types import JsonObject, json_as_str, json_loads_value

from .registry import ToolArguments, ToolArgumentValue


def _string_config_value(config: dict[str, ToolArgumentValue], name: str) -> str | None:
    value = config.pop(name, None)
    return value if isinstance(value, str) else None


async def _execute_via_smithery_connect(
    mcp_url: str,
    tool_name: str,
    arguments: ToolArguments,
    config: dict[str, ToolArgumentValue],
    agent_id: uuid.UUID | None = None,
) -> str:
    """Execute an MCP tool via Smithery Connect API.

    Uses stored namespace/connection or falls back to creating one.
    Smithery Connect returns SSE-format responses that need special parsing.
    """
    import json as json_mod

    import httpx

    # Get Smithery API key centrally (from discover_resources/import_mcp_server AgentTool config)
    from app.services.resource_discovery import _get_smithery_api_key

    api_key = await _get_smithery_api_key(agent_id)
    if not api_key:
        return (
            "❌ Smithery API key not configured.\n\n"
            + "Provide your Smithery API key. Get one by following these steps:\n"
            + "1. Sign up or sign in at https://smithery.ai\n"
            + "2. Open https://smithery.ai/account/api-keys and create an API key\n"
            + "3. Provide the key and I will configure it"
        )

    # Get namespace + connection from tool config, or use defaults
    namespace = _string_config_value(config, "smithery_namespace")
    connection_id = _string_config_value(config, "smithery_connection_id")

    if not namespace or not connection_id:
        # Fallback: try to get from Smithery settings
        try:
            from app.dao.tool_dao import tool_dao

            disc_tool = await tool_dao.get_by_name("discover_resources")
            if disc_tool and disc_tool.config:
                stored_namespace = disc_tool.config.get("smithery_namespace")
                stored_connection_id = disc_tool.config.get("smithery_connection_id")
                namespace = namespace or (stored_namespace if isinstance(stored_namespace, str) else None)
                connection_id = connection_id or (
                    stored_connection_id if isinstance(stored_connection_id, str) else None
                )
        except Exception:
            _ = None

    if not namespace or not connection_id:
        return (
            "❌ Smithery Connect namespace/connection not configured. "
            + "Please set smithery_namespace and smithery_connection_id in the tool configuration."
        )

    # Smithery Connect (and many MCP servers) emit SSE responses for tools/call.
    # The server returns 406 Not Acceptable if the client doesn't declare both
    # application/json and text/event-stream in the Accept header. We parse
    # both formats below, so advertise both.
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            # Call the tool via the existing connection
            tool_resp = await client.post(
                f"https://api.smithery.ai/connect/{namespace}/{connection_id}/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": tool_name,
                        "arguments": arguments,
                    },
                },
                headers=headers,
            )

            # Detect auth/connection failures and attempt auto-recovery
            if tool_resp.status_code in (401, 403, 404):
                recovery_result = await _smithery_auto_recover(api_key, mcp_url, namespace, connection_id, agent_id)
                if recovery_result:
                    return recovery_result
                # If recovery returned None, fall through to normal parsing

            # Smithery Connect returns SSE format: "event: message\ndata: {...}\n"
            raw = tool_resp.text
            data: JsonObject | None = None

            # Parse SSE response
            for line in raw.split("\n"):
                line = line.strip()
                if line.startswith("data: "):
                    try:
                        parsed = json_loads_value(line[6:])
                        data = parsed if isinstance(parsed, dict) else None
                        break
                    except json_mod.JSONDecodeError:
                        pass

            # Fallback: try parsing as plain JSON
            if data is None:
                try:
                    parsed = json_loads_value(raw)
                    if not isinstance(parsed, dict):
                        return f"❌ Unexpected response from Smithery: {raw[:300]}"
                    data = parsed
                except json_mod.JSONDecodeError:
                    return f"❌ Unexpected response from Smithery: {raw[:300]}"

            if "error" in data:
                err = data["error"]
                msg = json_as_str(err.get("message")) if isinstance(err, dict) else str(err)
                if not msg:
                    msg = str(err)
                # Check if error indicates auth/connection issue
                auth_keywords = ["auth", "unauthorized", "forbidden", "expired", "not found", "connection"]
                if any(kw in msg.lower() for kw in auth_keywords):
                    recovery_result = await _smithery_auto_recover(api_key, mcp_url, namespace, connection_id, agent_id)
                    if recovery_result:
                        return recovery_result
                return f"❌ MCP tool error: {msg[:300]}"

            result = data.get("result", {})
            if isinstance(result, str):
                return result

            raw_blocks = result.get("content") if isinstance(result, dict) else None
            content_blocks = list[object](raw_blocks) if isinstance(raw_blocks, list) else []
            texts: list[str] = []
            for block in content_blocks:
                if isinstance(block, str):
                    texts.append(block)
                elif isinstance(block, dict):
                    if block.get("type") == "text":
                        text = block.get("text", "")
                        texts.append(text if isinstance(text, str) else str(text))
                    elif block.get("type") == "image":
                        texts.append(f"[Image: {block.get('mimeType', 'image')}]")
                    else:
                        texts.append(str(block))
                else:
                    texts.append(str(block))

            return "\n".join(texts) if texts else str(result)

    except Exception as e:
        return f"❌ Smithery Connect error: {str(e)[:200]}"


async def _smithery_auto_recover(
    api_key: str, mcp_url: str, namespace: str, connection_id: str, agent_id: uuid.UUID | None = None
) -> str | None:
    """Attempt to auto-recover a failed Smithery connection.

    Re-creates the Smithery Connect connection. If OAuth is needed,
    returns the auth URL for the user. Returns None if recovery fails silently.
    """
    try:
        from app.services.resource_discovery import _ensure_smithery_connection

        display_name = connection_id.replace("-", " ").title() if connection_id else "MCP Server"

        conn_result = await _ensure_smithery_connection(api_key, mcp_url, display_name)
        if "error" in conn_result:
            return (
                f"❌ MCP tool connection expired and auto-recovery failed: {conn_result['error']}\n\n"
                + f'💡 Please re-authorize by telling me: `import_mcp_server(server_id="...", reauthorize=true)`'
            )

        auth_url = conn_result.get("auth_url")
        if auth_url:
            # A newly-created Smithery connection is not usable until the user
            # completes OAuth. Keep the existing stored connection in place so
            # a still-valid old connection is not overwritten by an unauthenticated
            # replacement. The user-facing auth URL is enough for recovery.
            return (
                f"🔐 MCP tool connection expired. Re-authorization needed.\n\n"
                + f"Please visit the following URL to re-authorize:\n"
                + f"{auth_url}\n\n"
                + f"After completing authorization, the tools will work again automatically."
            )

        # Update stored config with new connection info
        new_config = {
            "smithery_namespace": conn_result["namespace"],
            "smithery_connection_id": conn_result["connection_id"],
        }
        if agent_id:
            try:
                from app.dao.tool_dao import agent_tool_dao, tool_dao

                for tool in await tool_dao.list_mcp_by_server_url(mcp_url):
                    at = await agent_tool_dao.get_assignment(agent_id, tool.id)
                    if at:
                        _ = await agent_tool_dao.update(
                            db_obj=at,
                            obj_in={"config": {**(at.config or {}), **new_config}},
                        )
            except Exception:
                _ = None

        # Connection re-created without OAuth - should work now
        return None  # Signal caller to retry (but we don't retry here to avoid loops)

    except Exception as e:
        return f"❌ Auto-recovery failed: {str(e)[:200]}"
