"""MCP (Model Context Protocol) Client - connects to external MCP servers.

Supports two transport modes:
1. Streamable HTTP (modern) - single URL, POST JSON-RPC, response as JSON or SSE
2. SSE Transport (legacy but widely used) - GET /sse for event stream, POST /messages for requests

Transport is auto-detected: tries Streamable HTTP first, falls back to SSE.
Reference: https://modelcontextprotocol.io/docs
"""

import json
from collections.abc import Mapping
from contextlib import suppress
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import httpx

from app.core.json_types import (
    JsonObject,
    JsonValue,
    is_json_object,
    json_loads_value,
    json_value_from_response,
)
from app.core.logging import logger


def _require_json_object(value: object) -> JsonObject:
    if not is_json_object(value):
        raise TypeError("MCP response must be a JSON object")
    return value


class MCPClient:
    """Client for connecting to MCP servers via Streamable HTTP or SSE transport.

    Auto-detects the transport mode on first request.
    """

    def __init__(self, server_url: str, api_key: str | None = None):
        # Extract apiKey from URL query params and move to Authorization header
        parsed = urlparse(server_url)
        qs = parse_qs(parsed.query, keep_blank_values=True)

        self.api_key: str | None = api_key
        if not self.api_key and "apiKey" in qs:
            self.api_key = qs.pop("apiKey")[0]

        # Rebuild URL without apiKey in query string
        remaining_qs = urlencode({k: v[0] for k, v in qs.items()}) if qs else ""
        self.server_url: str = urlunparse(parsed._replace(query=remaining_qs)).rstrip("/")

        # Transport state
        self._transport: str | None = None  # "streamable" or "sse"
        self._session_id: str | None = None
        self._sse_messages_url: str | None = None  # POST endpoint for SSE transport

    def _headers(self) -> dict[str, str]:
        """Build request headers with proper MCP and auth headers."""
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    def _parse_response(self, resp: httpx.Response) -> JsonObject:
        """Parse response - handles both JSON and SSE (text/event-stream) formats."""
        content_type = resp.headers["content-type"] if "content-type" in resp.headers else ""

        # Save session ID if the server returns one
        if "mcp-session-id" in resp.headers:
            self._session_id = resp.headers["mcp-session-id"]

        if "text/event-stream" in content_type:
            return self._parse_sse_response(resp.text)
        return _require_json_object(json_value_from_response(resp))

    def _parse_sse_response(self, text: str) -> JsonObject:
        """Extract the last JSON-RPC result from an SSE stream."""
        last_data: JsonObject | None = None
        for line in text.splitlines():
            if line.startswith("data:"):
                raw = line[5:].strip()
                if raw and raw != "[DONE]":
                    with suppress(json.JSONDecodeError):
                        data = json_loads_value(raw)
                        if is_json_object(data):
                            last_data = data
        if last_data is None:
            raise Exception("No valid JSON found in SSE response")
        return last_data

    # ── Streamable HTTP Transport ────────────────────────────────

    async def _streamable_initialize(self, client: httpx.AsyncClient) -> None:
        """Send MCP initialize + initialized handshake (Streamable HTTP)."""
        try:
            resp = await client.post(
                self.server_url,
                json={
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "maraclaw", "version": "1.0"},
                    },
                },
                headers=self._headers(),
            )
            if resp.status_code == 200:
                _ = self._parse_response(resp)  # captures Mcp-Session-Id if present
            # Send initialized notification (required by MCP spec before other requests)
            _ = await client.post(
                self.server_url,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=self._headers(),
            )
        except Exception as exc:
            logger.debug("MCP streamable initialization failed; continuing without a session: {}", exc)

    async def _streamable_request(self, method: str, params: Mapping[str, JsonValue] | None = None) -> JsonObject:
        """Send a JSON-RPC request via Streamable HTTP transport."""
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            if not self._session_id:
                await self._streamable_initialize(client)

            body: JsonObject = {"jsonrpc": "2.0", "id": 1, "method": method, "params": dict(params or {})}

            resp = await client.post(self.server_url, json=body, headers=self._headers())
            if resp.status_code not in (200, 201):
                raise Exception(f"HTTP {resp.status_code}")
            return self._parse_response(resp)

    # ── SSE Transport ────────────────────────────────────────────

    async def _sse_connect(self) -> str:
        """Connect to SSE endpoint (GET /sse) and extract the messages URL.

        Returns the full POST URL for sending JSON-RPC messages.
        """
        # Determine SSE URL: if server_url ends with /sse use it directly,
        # otherwise append /sse
        sse_url = self.server_url if self.server_url.endswith("/sse") else f"{self.server_url}/sse"
        parsed = urlparse(sse_url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"

        headers = {"Accept": "text/event-stream"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        messages_url = None
        event_type = ""

        async with (
            httpx.AsyncClient(timeout=15, follow_redirects=True) as client,
            client.stream("GET", sse_url, headers=headers) as resp,
        ):
            if resp.status_code != 200:
                raise Exception(f"SSE connect failed: HTTP {resp.status_code}")

            # Read SSE events until we get the endpoint event
            async for line in resp.aiter_lines():
                line = line.strip()
                if line.startswith("event:"):
                    event_type = line[6:].strip()
                elif line.startswith("data:"):
                    data = line[5:].strip()
                    if event_type == "endpoint" and data:
                        # data is typically a relative URL like /messages?sessionId=xxx
                        messages_url = data if data.startswith("http") else base_url + data
                        break

        if not messages_url:
            raise Exception("SSE endpoint did not return a messages URL")

        return messages_url

    async def _sse_request(self, method: str, params: Mapping[str, JsonValue] | None = None) -> JsonObject:
        """Send a JSON-RPC request via SSE transport.

        Opens a fresh SSE connection each call to get the messages endpoint,
        sends the JSON-RPC request, then reads responses from the SSE stream.
        """
        # Connect to SSE to get the messages endpoint
        sse_url = self.server_url if self.server_url.endswith("/sse") else f"{self.server_url}/sse"
        parsed = urlparse(sse_url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"

        headers_sse: dict[str, str] = {"Accept": "text/event-stream"}
        headers_post: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.api_key:
            headers_sse["Authorization"] = f"Bearer {self.api_key}"
            headers_post["Authorization"] = f"Bearer {self.api_key}"

        body: JsonObject = {"jsonrpc": "2.0", "id": 1, "method": method, "params": dict(params or {})}

        timeout = 60 if method == "tools/call" else 30

        async with (
            httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client,
            client.stream("GET", sse_url, headers=headers_sse) as sse_resp,
        ):
            if sse_resp.status_code != 200:
                raise Exception(f"SSE connect failed: HTTP {sse_resp.status_code}")

            messages_url = None
            event_type = ""

            # Phase 1: Read until we get the endpoint event
            line_iter = sse_resp.aiter_lines()
            async for line in line_iter:
                line = line.strip()
                if line.startswith("event:"):
                    event_type = line[6:].strip()
                elif line.startswith("data:"):
                    data = line[5:].strip()
                    if event_type == "endpoint" and data:
                        messages_url = data if data.startswith("http") else base_url + data
                        break

            if not messages_url:
                raise Exception("SSE endpoint did not return a messages URL")

            # Phase 2: MCP handshake - initialize + initialized notification
            init_body: JsonObject = {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "maraclaw", "version": "1.0"},
                },
            }
            _ = await client.post(messages_url, json=init_body, headers=headers_post)
            # Send initialized notification (required before other requests)
            _ = await client.post(
                messages_url,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=headers_post,
            )

            # Send the actual request
            post_resp = await client.post(messages_url, json=body, headers=headers_post)

            # Phase 3: Read the response - either from POST response or from SSE stream
            if post_resp.status_code == 200:
                ct = post_resp.headers["content-type"] if "content-type" in post_resp.headers else ""
                if "application/json" in ct:
                    return _require_json_object(json_value_from_response(post_resp))

            # Read response from SSE stream
            result: JsonObject | None = None
            async for line in line_iter:
                line = line.strip()
                if line.startswith("event:"):
                    event_type = line[6:].strip()
                elif line.startswith("data:"):
                    data = line[5:].strip()
                    if event_type == "message" and data:
                        with suppress(json.JSONDecodeError):
                            parsed_data = json_loads_value(data)
                            # Match our request ID
                            if is_json_object(parsed_data) and parsed_data.get("id") in (0, 1):
                                result = parsed_data
                                if parsed_data.get("id") == 1:
                                    break  # Got our actual request response

            if result is None:
                raise Exception("No response received from SSE transport")
            return result

    # ── Auto-detect Transport ────────────────────────────────────

    async def _detect_and_request(self, method: str, params: Mapping[str, JsonValue] | None = None) -> JsonObject:
        """Auto-detect transport and send request.

        Strategy: If transport is already known, use it directly.
        Otherwise try Streamable HTTP first, fall back to SSE.
        """
        if self._transport == "sse":
            return await self._sse_request(method, params)
        if self._transport == "streamable":
            return await self._streamable_request(method, params)

        # Auto-detect: try Streamable HTTP first. Python clears exception
        # variables after an `except ... as name` block exits, so keep a stable
        # string copy for the later SSE fallback error.
        streamable_error_message = ""
        try:
            result = await self._streamable_request(method, params)
            self._transport = "streamable"
            return result
        except Exception as streamable_err:
            streamable_error_message = str(streamable_err)
            logger.info(f"[MCPClient] Streamable HTTP failed ({streamable_err}), trying SSE transport...")

        # Fallback to SSE
        try:
            result = await self._sse_request(method, params)
            self._transport = "sse"
            return result
        except Exception as sse_err:
            raise Exception(
                f"Both transports failed. Streamable HTTP: {streamable_error_message}; SSE: {sse_err}"
            ) from sse_err

    # ── Public API ───────────────────────────────────────────────

    async def list_tools(self) -> list[JsonObject]:
        """Fetch available tools from the MCP server."""
        try:
            data = await self._detect_and_request("tools/list")

            if "error" in data:
                err = data["error"]
                error_message = err.get("message", str(err)) if is_json_object(err) else str(err)
                msg = error_message if isinstance(error_message, str) else str(error_message)
                raise Exception(f"MCP error: {msg}")

            result = data.get("result", {})
            if not is_json_object(result):
                return []
            tools = result.get("tools", [])
            if not isinstance(tools, list):
                return []
            return [
                {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "inputSchema": tool.get("inputSchema", {}),
                }
                for tool in tools
                if is_json_object(tool)
            ]
        except httpx.HTTPError as e:
            raise Exception(f"Connection failed: {str(e)[:200]}") from e

    async def call_tool(self, tool_name: str, arguments: Mapping[str, JsonValue]) -> str:
        """Execute a tool on the MCP server."""
        try:
            data = await self._detect_and_request(
                "tools/call",
                {"name": tool_name, "arguments": dict(arguments)},
            )

            if "error" in data:
                err = data["error"]
                error_message = err.get("message", str(err)) if is_json_object(err) else str(err)
                msg = error_message if isinstance(error_message, str) else str(error_message)
                return f"❌ MCP tool execution error: {msg[:200]}"

            result = data.get("result", {})
            if isinstance(result, str):
                return result

            # MCP returns content as list of content blocks
            raw_blocks = result.get("content") if is_json_object(result) else None
            if not isinstance(raw_blocks, list):
                return str(result)
            content_blocks: list[object] = list(raw_blocks)
            texts: list[str] = []
            for block in content_blocks:
                if isinstance(block, str):
                    texts.append(block)
                elif is_json_object(block):
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

        except httpx.HTTPError as e:
            return f"❌ MCP connection failed: {str(e)[:200]}"
