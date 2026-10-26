from dataclasses import dataclass

import httpx

from app.core.json_types import (
    JsonObject,
    is_json_object,
    json_as_str,
    json_loads_value,
    json_object_from,
    json_object_from_response,
)
from app.records.agent_setup_mcp import MCPWarningCode


@dataclass(frozen=True, slots=True)
class SmitheryServer:
    qualified_name: str
    display_name: str
    description: str
    remote: bool
    url: str
    tools: list[JsonObject]


async def discover_server(server_id: str) -> SmitheryServer | None:
    from app.services import resource_discovery as discovery

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        response = await client.get(
            f"{discovery.SMITHERY_API_BASE}/servers",
            params={"q": server_id.lstrip("@"), "pageSize": 5},
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        raw = json_object_from_response(response).get("servers", [])
        servers = [item for item in raw if is_json_object(item)] if isinstance(raw, list) else []
        server = next(
            (item for item in servers if item.get("qualifiedName") in (server_id, server_id.lstrip("@"))), None
        )
        if server is None:
            server = servers[0] if servers else None
        if server is None:
            return None
        qualified = json_as_str(server.get("qualifiedName")) or server_id.lstrip("@")
        detail: JsonObject = {}
        try:
            response = await client.get(f"{discovery.SMITHERY_API_BASE}/servers/{qualified}")
            response.raise_for_status()
            detail = json_object_from_response(response)
        except httpx.HTTPError, ValueError:
            pass
        raw_tools = detail.get("tools", [])
        tools = [item for item in raw_tools if is_json_object(item)] if isinstance(raw_tools, list) else []
        return SmitheryServer(
            qualified,
            json_as_str(server.get("displayName")) or server_id.split("/")[-1],
            json_as_str(server.get("description")) or "",
            bool(server.get("remote")),
            json_as_str(detail.get("deploymentUrl")) or f"https://{qualified}.run.tools",
            tools,
        )


async def live_tools(api_key: str, connection: JsonObject) -> tuple[list[JsonObject], MCPWarningCode | None]:
    namespace = connection["smithery_namespace"]
    connection_id = connection["smithery_connection_id"]
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"https://api.smithery.ai/connect/{namespace}/{connection_id}/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            )
        if response.status_code in (401, 403):
            return [], "authorization_required"
        response.raise_for_status()
        payload: JsonObject = {}
        for text in [line[6:] for line in response.text.splitlines() if line.startswith("data: ")] + [response.text]:
            try:
                parsed = json_loads_value(text)
            except ValueError:
                continue
            if is_json_object(parsed):
                payload = parsed
                break
        raw_tools = json_object_from(payload.get("result")).get("tools", [])
        tools = (
            [item for item in raw_tools if is_json_object(item) and is_json_object(item.get("inputSchema"))]
            if isinstance(raw_tools, list)
            else []
        )
        if tools:
            return tools, None
        error = json_object_from(payload.get("error"))
        if error.get("code") in (401, 403):
            return [], "authorization_required"
        return [], "tools_unverified"
    except httpx.HTTPError:
        return [], "connection_failed"
