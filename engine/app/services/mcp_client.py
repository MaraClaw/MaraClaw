"""MCP (Model Context Protocol) Client - connects to external MCP servers.

Supports two transport modes:
1. Streamable HTTP (modern) - single URL, POST JSON-RPC, response as JSON or SSE
2. SSE Transport (legacy but widely used) - GET /sse for event stream, POST /messages for requests

Transport is auto-detected: Streamable HTTP first, then SSE. Discovery (``tools/list``) is idempotent and falls back
after any streamable failure; ``tools/call`` falls back only when the server refused the streamable ``initialize``,
before the call was sent, so a tool never runs twice. Only that refusal pins a client to SSE.

Agent tool calls reuse one initialized session per agent, server URL and credential through ``mcp_session_pool``;
calls without an agent, probes, catalog import, Atlassian and discovery build per-call clients. Every response body
is read under ``MCP_MAX_RESPONSE_BYTES`` before it is parsed, and every ``tools/call`` runs under one overall
``MCP_TOOL_CALL_DEADLINE_SECONDS`` deadline, which keepalive pings on an event stream cannot extend.
Reference: https://modelcontextprotocol.io/docs
"""

import asyncio
import codecs
import hashlib
import hmac
import io
import json
import secrets
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping
from contextlib import aclosing, asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Literal, override
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import httpx

from app.core.json_types import JsonObject, JsonValue, http_header, is_json_object, json_loads_value
from app.core.logging import logger
from app.core.outbound_policy import OutboundPolicyError, ensure_same_origin
from app.core.outbound_transport import public_http_client
from app.services.mcp_inventory import MCPInventoryError, complete_inventory

MCP_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MCP_MAX_REDIRECT_BODY_BYTES = 16 * 1024
MCP_MAX_TOOL_TEXT_CHARS = 100_000
MCP_TOOL_TEXT_TRUNCATION_MARKER = f"[MCP tool output truncated to {MCP_MAX_TOOL_TEXT_CHARS} characters]"
MCP_TOOL_CALL_DEADLINE_SECONDS = 300.0
MCP_TOOL_DEADLINE_EXCEEDED_TEXT = (
    f"❌ MCP tool call exceeded its {MCP_TOOL_CALL_DEADLINE_SECONDS:g} second deadline and was not retried; "
    "the server may still have run it"
)
MCP_SESSION_POOL_MAX_ENTRIES = 256
MCP_SESSION_IDLE_TTL_SECONDS = 300.0
MCP_SESSION_SHUTDOWN_TIMEOUT_SECONDS = 2.0

_REQUEST_TIMEOUT_SECONDS = 30.0
_SSE_CONNECT_TIMEOUT_SECONDS = 15.0
_SSE_TOOL_CALL_TIMEOUT_SECONDS = 60.0
# A server without a streamable endpoint answers initialize with one of these.
_STREAMABLE_REFUSALS = frozenset({404, 405, 406, 415})
_IDEMPOTENT_METHODS = frozenset({"tools/list"})
_LINE_BREAKS = "\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"
# Per-process HMAC key: credential fingerprints are comparable inside this process only.
_PROCESS_SALT = secrets.token_bytes(32)
_INITIALIZED_NOTIFICATION: JsonObject = {"jsonrpc": "2.0", "method": "notifications/initialized"}

type Transport = Literal["streamable", "sse"]


def _require_json_object(value: object) -> JsonObject:
    if not is_json_object(value):
        raise TypeError("MCP response must be a JSON object")
    return value


def _initialize_body(request_id: int) -> JsonObject:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "maraclaw", "version": "1.0"},
        },
    }


def _host(url: str) -> str:
    return urlparse(url).hostname or "unknown host"


def _normalize_target(server_url: str, api_key: str | None) -> tuple[str, str | None]:
    """Move an embedded ``?apiKey=`` into the credential (an explicit key wins) and drop a trailing slash."""
    parsed = urlparse(server_url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    embedded_key = query.pop("apiKey", None)
    if api_key in (None, "") and embedded_key is not None:
        api_key = embedded_key[0]
    remaining = urlencode({name: values[0] for name, values in query.items()}) if query else ""
    return urlunparse(parsed._replace(query=remaining)).rstrip("/"), api_key


def _credential_fingerprint(api_key: str | None) -> str:
    if api_key is None or api_key == "":
        return ""
    return hmac.new(_PROCESS_SALT, api_key.encode(), hashlib.sha256).hexdigest()[:32]


def cap_tool_text(text: str) -> str:
    """The tool text every MCP result path returns: at most ``MCP_MAX_TOOL_TEXT_CHARS`` characters plus a marker."""
    if len(text) <= MCP_MAX_TOOL_TEXT_CHARS:
        return text
    return f"{text[:MCP_MAX_TOOL_TEXT_CHARS]}\n{MCP_TOOL_TEXT_TRUNCATION_MARKER}"


class MCPClientError(Exception):
    """MCP protocol or connection failure reported by the client boundary."""


class MCPResponseTooLargeError(MCPClientError):
    """A response body exceeded its byte cap and was refused before anything buffered or parsed the rest."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"MCP response exceeded {limit} bytes and was not parsed")


class _StreamableUnsupportedError(MCPClientError):
    """The server refused the streamable ``initialize``; nothing ran yet, so SSE may be tried and pinned."""


class _SessionExpiredError(MCPClientError):
    """A request carrying ``Mcp-Session-Id`` got 404: the server no longer knows that session."""


@dataclass(slots=True)
class _ByteBudget:
    limit: int
    used: int = 0

    def charge(self, size: int) -> None:
        self.used += size
        if self.used > self.limit:
            raise MCPResponseTooLargeError(self.limit)


@dataclass(frozen=True, slots=True)
class _Reply:
    status: int
    headers: httpx.Headers
    body: bytes


def _refuse_declared_size(headers: httpx.Headers, limit: int) -> None:
    declared = http_header(headers, "content-length")
    if declared.isdigit() and int(declared) > limit:
        raise MCPResponseTooLargeError(limit)


async def _read_capped(response: httpx.Response, limit: int) -> bytes:
    """Read a streamed body, refusing a declared or actual size above ``limit`` before anything parses it."""
    _refuse_declared_size(response.headers, limit)
    budget = _ByteBudget(limit)
    body = bytearray()
    async for chunk in response.aiter_bytes():
        budget.charge(len(chunk))
        body += chunk
    return bytes(body)


class _LineSplitter:
    """Incremental ``str.splitlines``: each decoded character is scanned once.

    An unterminated line accumulates in a ``StringIO`` instead of being re-joined and re-split with every chunk, so a
    server trickling one long line costs linear time rather than stalling the event loop.
    """

    def __init__(self) -> None:
        self._held: io.StringIO = io.StringIO()
        self._carry_cr: bool = False

    def feed(self, text: str) -> list[str]:
        if self._carry_cr:
            text = "\r" + text
        # A trailing "\r" may be the first half of "\r\n"; the next piece decides.
        self._carry_cr = text.endswith("\r")
        if self._carry_cr:
            text = text[:-1]
        lines: list[str] = []
        for part in text.splitlines(keepends=True):
            if part[-1] in _LINE_BREAKS:
                self._held.write(part.rstrip(_LINE_BREAKS))
                lines.append(self._held.getvalue())
                self._held = io.StringIO()
            else:
                _ = self._held.write(part)
        return lines

    def flush(self) -> list[str]:
        """The final line: unterminated text, or the empty line a stream-ending ``"\\r"`` terminated."""
        if not (self._held.tell() or self._carry_cr):
            return []
        return [self._held.getvalue()]


async def _capped_lines(response: httpx.Response, budget: _ByteBudget) -> AsyncGenerator[str]:
    """Yield lines like ``aiter_lines`` (``str.splitlines`` breaks), charging every byte before it is buffered."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    splitter = _LineSplitter()
    async for chunk in response.aiter_bytes():
        budget.charge(len(chunk))
        for line in splitter.feed(decoder.decode(chunk)):
            yield line
    for line in [*splitter.feed(decoder.decode(b"", final=True)), *splitter.flush()]:
        yield line


class _CappedRedirectBody(httpx.AsyncByteStream):
    """A redirect body that stops before yielding past ``limit`` bytes, so whoever reads it stays bounded."""

    def __init__(self, inner: httpx.AsyncByteStream, limit: int) -> None:
        self._inner: httpx.AsyncByteStream = inner
        self._limit: int = limit

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        budget = _ByteBudget(self._limit)
        async for chunk in self._inner:
            budget.charge(len(chunk))
            yield chunk

    @override
    async def aclose(self) -> None:
        await self._inner.aclose()


async def _cap_redirect_body(response: httpx.Response) -> None:
    """Response hook: bound the body of every redirect httpx is about to follow.

    httpx ``aread``s each intermediate 3xx body before returning the final response, so neither ``_read_capped`` nor
    ``_capped_lines`` ever sees it. Response hooks run before that read: a redirect whose declared Content-Length
    exceeds ``MCP_MAX_REDIRECT_BODY_BYTES`` is refused unread, and any other redirect stream is wrapped to stop there.
    """
    if not response.has_redirect_location:
        return
    _refuse_declared_size(response.headers, MCP_MAX_REDIRECT_BODY_BYTES)
    if isinstance(response.stream, httpx.AsyncByteStream):
        response.stream = _CappedRedirectBody(response.stream, MCP_MAX_REDIRECT_BODY_BYTES)


def _mcp_http_client(timeout_seconds: float, origin: str) -> httpx.AsyncClient:
    """Build every MCP HTTP client (pooled, per-call, SSE, DELETE): ``public_http_client`` plus capped redirect bodies.

    Redirects are left to httpx rather than followed by hand: servers mounted at ``/mcp/`` answer the slash-stripped
    ``/mcp`` with a 307, and httpx already applies the method and body rules (307/308 keep both, 303 and a POST
    answered by 301/302 become GET), strips credentials on an origin change and stops after ``max_redirects`` hops,
    while ``public_http_client``'s transport checks the bound origin on every hop. Its one unbounded step, reading
    each intermediate 3xx body into memory, is capped by the ``_cap_redirect_body`` response hook.
    """
    http = public_http_client(timeout=timeout_seconds, follow_redirects=True, origin=origin)
    hooks = http.event_hooks
    http.event_hooks = {"request": hooks["request"], "response": [*hooks["response"], _cap_redirect_body]}
    return http


async def _post(
    http: httpx.AsyncClient, url: str, body: JsonObject, headers: Mapping[str, str], timeout_seconds: float
) -> _Reply:
    async with http.stream("POST", url, json=body, headers=headers, timeout=timeout_seconds) as response:
        return _Reply(response.status_code, response.headers, await _read_capped(response, MCP_MAX_RESPONSE_BYTES))


class MCPClient:
    """Client for connecting to MCP servers via Streamable HTTP or SSE transport.

    Auto-detects the transport mode on first request. ``http`` lends a long-lived ``_mcp_http_client`` (the session
    pool's), which this client never closes; without it every request opens and closes its own client.
    """

    def __init__(self, server_url: str, api_key: str | None = None, *, http: httpx.AsyncClient | None = None):
        url, key = _normalize_target(server_url, api_key)
        self.server_url: str = url
        self.api_key: str | None = key
        self._http: httpx.AsyncClient | None = http
        self._transport: Transport | None = None
        self._session_id: str | None = None
        self._initialized: bool = False
        self._init_lock: asyncio.Lock = asyncio.Lock()
        self._request_id: int = 0

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def _next_request_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _headers(self, session_id: str | None = None) -> dict[str, str]:
        """Build request headers with proper MCP and auth headers."""
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.api_key not in (None, ""):
            h["Authorization"] = f"Bearer {self.api_key}"
        if session_id not in (None, ""):
            h["Mcp-Session-Id"] = session_id
        return h

    def _sse_headers(self) -> dict[str, str]:
        headers = {"Accept": "text/event-stream"}
        if self.api_key not in (None, ""):
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _sse_url(self) -> str:
        """Use ``server_url`` directly when it ends with /sse, otherwise append /sse."""
        return self.server_url if self.server_url.endswith("/sse") else f"{self.server_url}/sse"

    @asynccontextmanager
    async def _client(self, timeout_seconds: float) -> AsyncGenerator[httpx.AsyncClient]:
        """Yield the lent client without closing it, or a per-call ``_mcp_http_client`` closed on exit."""
        if self._http is not None:
            yield self._http
            return
        async with _mcp_http_client(timeout_seconds, self.server_url) as client:
            yield client

    def _parse_response(self, headers: httpx.Headers, body: bytes) -> JsonObject:
        """Parse an already capped body - handles both JSON and SSE (text/event-stream) formats."""
        if "text/event-stream" in http_header(headers, "content-type"):
            return self._parse_sse_response(body.decode("utf-8", errors="replace"))
        return _require_json_object(json_loads_value(body))

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
            raise MCPClientError("No valid JSON found in SSE response")
        return last_data

    # ── Streamable HTTP Transport ────────────────────────────────

    async def _streamable_initialize(self, client: httpx.AsyncClient) -> None:
        """Send MCP initialize + initialized handshake (Streamable HTTP).

        Succeeds only on a 2xx JSON or event-stream reply, then stores ``Mcp-Session-Id``. A refusal status, or a 2xx
        that is neither, raises ``_StreamableUnsupportedError``; any other failure raises and leaves no session behind.
        """
        reply = await _post(
            client,
            self.server_url,
            _initialize_body(self._next_request_id()),
            self._headers(),
            _REQUEST_TIMEOUT_SECONDS,
        )
        content_type = http_header(reply.headers, "content-type")
        is_success = 200 <= reply.status < 300
        if reply.status in _STREAMABLE_REFUSALS or (
            is_success and "application/json" not in content_type and "text/event-stream" not in content_type
        ):
            raise _StreamableUnsupportedError(f"Streamable initialize refused: HTTP {reply.status}")
        if not is_success:
            raise MCPClientError(f"MCP initialize failed: HTTP {reply.status}")
        try:
            initialized = self._parse_response(reply.headers, reply.body)
        except TypeError, ValueError:
            raise MCPClientError("MCP initialize returned an invalid reply") from None
        if "error" in initialized:
            raise MCPClientError("MCP initialize was rejected")
        session_id = http_header(reply.headers, "mcp-session-id") or None
        # Required by the MCP spec before other requests; servers answer 202 and nothing depends on the body.
        notified = await _post(
            client, self.server_url, _INITIALIZED_NOTIFICATION, self._headers(session_id), _REQUEST_TIMEOUT_SECONDS
        )
        if not 200 <= notified.status < 300:
            logger.debug(
                "[MCPClient] {} answered notifications/initialized with HTTP {}",
                _host(self.server_url),
                notified.status,
            )
        self._session_id = session_id

    async def _ensure_session(self, client: httpx.AsyncClient, *, expired: str | None = None) -> None:
        """Initialize at most once at a time: double-checked under the lock, so concurrent first calls share one.

        ``expired`` is a session id that just got 404; it is replaced unless another caller already replaced it.
        """
        if self._initialized and (expired is None or self._session_id != expired):
            return
        async with self._init_lock:
            if self._initialized and (expired is None or self._session_id != expired):
                return
            self._initialized = False
            self._session_id = None
            await self._streamable_initialize(client)
            self._initialized = True

    async def _streamable_send(
        self,
        client: httpx.AsyncClient,
        method: str,
        params: Mapping[str, JsonValue] | None,
        session_id: str | None,
    ) -> JsonObject:
        body: JsonObject = {
            "jsonrpc": "2.0",
            "id": self._next_request_id(),
            "method": method,
            "params": dict(params or {}),
        }
        reply = await _post(client, self.server_url, body, self._headers(session_id), _REQUEST_TIMEOUT_SECONDS)
        if reply.status == 404 and session_id is not None:
            raise _SessionExpiredError("HTTP 404 for the MCP session")
        if reply.status not in (200, 201):
            raise MCPClientError(f"HTTP {reply.status}")
        return self._parse_response(reply.headers, reply.body)

    async def _streamable_request(self, method: str, params: Mapping[str, JsonValue] | None = None) -> JsonObject:
        """Send a JSON-RPC request via Streamable HTTP transport.

        A 404 for the session id that was sent drops that session, re-initializes and retries exactly once. Every
        other failure after the request was sent propagates and is never retried.
        """
        async with self._client(_REQUEST_TIMEOUT_SECONDS) as client:
            await self._ensure_session(client)
            session_id = self._session_id
            try:
                return await self._streamable_send(client, method, params, session_id)
            except _SessionExpiredError:
                logger.info("[MCPClient] MCP session on {} expired; re-initializing once", _host(self.server_url))
            try:
                await self._ensure_session(client, expired=session_id)
            except _StreamableUnsupportedError as refused:
                # The request already reached this endpoint once, so it must not move to SSE now.
                raise MCPClientError(str(refused)) from None
            return await self._streamable_send(client, method, params, self._session_id)

    # ── SSE Transport ────────────────────────────────────────────

    async def _sse_connect(self) -> str:
        """Connect to SSE endpoint (GET /sse) and extract the messages URL.

        Returns the full POST URL for sending JSON-RPC messages.
        """
        sse_url = self._sse_url()
        messages_url = None
        event_type = ""

        async with (
            self._client(_SSE_CONNECT_TIMEOUT_SECONDS) as client,
            client.stream("GET", sse_url, headers=self._sse_headers(), timeout=_SSE_CONNECT_TIMEOUT_SECONDS) as resp,
            aclosing(_capped_lines(resp, _ByteBudget(MCP_MAX_RESPONSE_BYTES))) as lines,
        ):
            if resp.status_code != 200:
                raise MCPClientError(f"SSE connect failed: HTTP {resp.status_code}")

            # Read SSE events until we get the endpoint event
            async for line in lines:
                line = line.strip()
                if line.startswith("event:"):
                    event_type = line[6:].strip()
                elif line.startswith("data:"):
                    data = line[5:].strip()
                    if event_type == "endpoint" and data:
                        # data is typically a relative URL like /messages?sessionId=xxx
                        messages_url = urljoin(sse_url, data)
                        ensure_same_origin(self.server_url, messages_url)
                        break

        if messages_url in (None, ""):
            raise MCPClientError("SSE endpoint did not return a messages URL")

        return messages_url

    async def _sse_request(self, method: str, params: Mapping[str, JsonValue] | None = None) -> JsonObject:
        """Send a JSON-RPC request via SSE transport.

        Keep cursor discovery inside one connection and initialized session. The legacy transport has no persistent
        listener here, so each request opens its own event stream and handshake, on the lent client when there is one.
        Each JSON-RPC response gets its own ``MCP_MAX_RESPONSE_BYTES`` budget.
        """
        sse_url = self._sse_url()
        headers_post = self._headers()
        timeout = _SSE_TOOL_CALL_TIMEOUT_SECONDS if method == "tools/call" else _REQUEST_TIMEOUT_SECONDS
        budget = _ByteBudget(MCP_MAX_RESPONSE_BYTES)

        async with (
            self._client(timeout) as client,
            client.stream("GET", sse_url, headers=self._sse_headers(), timeout=timeout) as sse_resp,
            aclosing(_capped_lines(sse_resp, budget)) as line_iter,
        ):
            if sse_resp.status_code != 200:
                raise MCPClientError(f"SSE connect failed: HTTP {sse_resp.status_code}")

            messages_url = None
            event_type = ""

            # Phase 1: Read until we get the endpoint event
            async for line in line_iter:
                line = line.strip()
                if line.startswith("event:"):
                    event_type = line[6:].strip()
                elif line.startswith("data:"):
                    data = line[5:].strip()
                    if event_type == "endpoint" and data:
                        messages_url = urljoin(sse_url, data)
                        ensure_same_origin(self.server_url, messages_url)
                        break

            if messages_url in (None, ""):
                raise MCPClientError("SSE endpoint did not return a messages URL")

            # Phase 2: MCP handshake - initialize + initialized notification
            _ = await _post(client, messages_url, _initialize_body(0), headers_post, timeout)
            # Send initialized notification (required before other requests)
            _ = await _post(client, messages_url, _INITIALIZED_NOTIFICATION, headers_post, timeout)

            request_id = 0

            async def exchange(method: str, parameters: JsonObject) -> JsonObject:
                nonlocal request_id, event_type
                request_id += 1
                budget.used = 0
                body: JsonObject = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": parameters}
                post_resp = await _post(client, messages_url, body, headers_post, timeout)
                if post_resp.status == 200 and "application/json" in http_header(post_resp.headers, "content-type"):
                    return _require_json_object(json_loads_value(post_resp.body))
                async for line in line_iter:
                    line = line.strip()
                    if line.startswith("event:"):
                        event_type = line[6:].strip()
                    elif line.startswith("data:") and event_type == "message":
                        with suppress(json.JSONDecodeError):
                            parsed = json_loads_value(line[5:].strip())
                            if is_json_object(parsed) and parsed.get("id") == request_id:
                                return parsed
                raise MCPClientError("No response received from SSE transport")

            if method == "tools/list":
                inventory: list[JsonValue] = list(await complete_inventory(exchange))
                return {"result": {"tools": inventory}}
            return await exchange(method, dict(params or {}))

    # ── Auto-detect Transport ────────────────────────────────────

    async def _detect_and_request(self, method: str, params: Mapping[str, JsonValue] | None = None) -> JsonObject:
        """Auto-detect transport and send request.

        Strategy: If transport is already known, use it directly. Otherwise try Streamable HTTP first. Idempotent
        discovery falls back to SSE after any streamable failure; other methods only after ``_StreamableUnsupportedError``
        (the request was never sent), which alone pins SSE. Policy errors always propagate.
        """
        if self._transport == "sse":
            return await self._sse_request(method, params)
        if self._transport == "streamable":
            return await self._streamable_request(method, params)

        # Python clears exception variables after an `except ... as name` block exits, so keep a stable
        # string copy for the later SSE fallback error.
        streamable_error_message = ""
        refused = False
        try:
            result = await self._streamable_request(method, params)
        except OutboundPolicyError:
            raise
        except _StreamableUnsupportedError as unsupported:
            refused = True
            streamable_error_message = str(unsupported)
        except Exception as streamable_err:
            if method not in _IDEMPOTENT_METHODS:
                raise
            streamable_error_message = str(streamable_err)
        else:
            self._transport = "streamable"
            return result
        logger.info("[MCPClient] Streamable HTTP unavailable on {}, trying SSE transport", _host(self.server_url))

        try:
            result = await self._sse_request(method, params)
        except OutboundPolicyError:
            raise
        except Exception as sse_err:
            raise MCPClientError(
                f"Both transports failed. Streamable HTTP: {streamable_error_message}; SSE: {sse_err}"
            ) from sse_err
        if refused:
            self._transport = "sse"
        return result

    # ── Public API ───────────────────────────────────────────────

    async def list_tools(self) -> list[JsonObject]:
        """Fetch and validate every page, or fail without returning a partial inventory."""
        try:
            return await complete_inventory(self._detect_and_request)
        except httpx.HTTPError, MCPInventoryError, MCPClientError, json.JSONDecodeError:
            raise MCPClientError("MCP discovery failed") from None

    async def end_session(self) -> int | None:
        """Ask the server to end the streamable session; returns the HTTP status, or ``None`` without a session."""
        if self._session_id is None:
            return None
        async with (
            self._client(_REQUEST_TIMEOUT_SECONDS) as client,
            client.stream("DELETE", self.server_url, headers=self._headers(self._session_id)) as response,
        ):
            return response.status_code

    async def call_tool(self, tool_name: str, arguments: Mapping[str, JsonValue]) -> str:
        """Execute a tool on the MCP server. Failures come back as error text; a sent call is never retried.

        The whole call, handshake included, runs under one ``MCP_TOOL_CALL_DEADLINE_SECONDS`` deadline on top of httpx's
        per-read timeouts, so pings on an event stream cannot hold it open. A call that runs out of time is reported as
        failed like any other, and is neither retried nor replayed.
        """
        deadline = asyncio.timeout(MCP_TOOL_CALL_DEADLINE_SECONDS)
        try:
            async with deadline:
                data = await self._detect_and_request(
                    "tools/call",
                    {"name": tool_name, "arguments": dict(arguments)},
                )
        except TimeoutError:
            if not deadline.expired():
                raise
            logger.warning(
                "[MCPClient] tools/call on {} exceeded its {} s deadline",
                _host(self.server_url),
                MCP_TOOL_CALL_DEADLINE_SECONDS,
            )
            return MCP_TOOL_DEADLINE_EXCEEDED_TEXT
        except httpx.HTTPError as e:
            logger.warning("[MCPClient] tools/call on {} failed: {}", _host(self.server_url), type(e).__name__)
            return f"❌ MCP connection failed: {(str(e) or type(e).__name__)[:200]}"
        except MCPClientError as e:
            logger.warning("[MCPClient] tools/call on {} failed: {}", _host(self.server_url), type(e).__name__)
            return f"❌ MCP tool execution error: {str(e)[:200]}"

        if "error" in data:
            err = data["error"]
            error_message = err.get("message", str(err)) if is_json_object(err) else str(err)
            msg = error_message if isinstance(error_message, str) else str(error_message)
            return f"❌ MCP tool execution error: {msg[:200]}"

        result = data.get("result", {})
        if isinstance(result, str):
            return cap_tool_text(result)

        # MCP returns content as list of content blocks
        raw_blocks = result.get("content") if is_json_object(result) else None
        if not isinstance(raw_blocks, list):
            return cap_tool_text(str(result))
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

        return cap_tool_text("\n".join(texts) if texts else str(result))


# ── Session pool ─────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MCPSessionKey:
    """Pool identity: agent, normalized server URL and a salted fingerprint of the credential, never the key itself."""

    agent_id: uuid.UUID
    url: str
    credential: str


@dataclass(eq=False, slots=True)
class _PooledSession:
    key: MCPSessionKey
    client: MCPClient
    http: httpx.AsyncClient
    loop: asyncio.AbstractEventLoop
    last_used: float
    in_use: int = 0


async def _end_session(entry: _PooledSession) -> None:
    """Best-effort DELETE; 404 (already gone) and 405 (not supported) are expected answers."""
    try:
        status = await entry.client.end_session()
    except Exception as exc:
        logger.debug(
            "[MCP] Ending the session of agent {} on {} failed: {}",
            entry.key.agent_id,
            _host(entry.key.url),
            type(exc).__name__,
        )
        return
    if status is not None and not 200 <= status < 300 and status not in (404, 405):
        logger.debug(
            "[MCP] Ending the session of agent {} on {} returned HTTP {}",
            entry.key.agent_id,
            _host(entry.key.url),
            status,
        )


async def _close_clients(entries: list[_PooledSession]) -> None:
    for entry in entries:
        try:
            await entry.http.aclose()
        except Exception as exc:
            logger.warning(
                "[MCP] Closing the pooled client of agent {} on {} failed: {}",
                entry.key.agent_id,
                _host(entry.key.url),
                exc,
            )


class MCPSessionPool:
    """Initialized MCP sessions per agent, normalized server URL and credential, bounded by count and idle time.

    Each entry owns one ``_mcp_http_client`` (DNS pinning on every new connection, origin/Host/SNI checks on every
    request and redirect hop, capped redirect bodies) that only eviction, idle expiry or shutdown closes. Entries belong to the event loop that created them:
    one found on another loop is dropped without awaiting its client. When every slot is busy, the call runs on an
    unpooled per-call client instead of evicting a busy session. SSE-only servers keep their pinned transport and
    pooled client, but each SSE call still opens its own event stream and handshake; reusing a session without a
    second ``initialize`` applies to streamable servers.
    """

    def __init__(
        self,
        *,
        max_entries: int = MCP_SESSION_POOL_MAX_ENTRIES,
        idle_ttl_seconds: float = MCP_SESSION_IDLE_TTL_SECONDS,
        shutdown_timeout_seconds: float = MCP_SESSION_SHUTDOWN_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_entries: int = max_entries
        self._idle_ttl_seconds: float = idle_ttl_seconds
        self._shutdown_timeout_seconds: float = shutdown_timeout_seconds
        self._clock: Callable[[], float] = clock
        self._entries: OrderedDict[MCPSessionKey, _PooledSession] = OrderedDict()

    async def call_tool(
        self,
        *,
        agent_id: uuid.UUID,
        server_url: str,
        api_key: str | None,
        tool_name: str,
        arguments: Mapping[str, JsonValue],
    ) -> str:
        url, key = _normalize_target(server_url, api_key)
        session_key = MCPSessionKey(agent_id=agent_id, url=url, credential=_credential_fingerprint(key))
        retired: list[_PooledSession] = []
        entry: _PooledSession | None = None
        try:
            entry = self._acquire(session_key, key, retired)
            if entry is None:
                logger.warning(
                    "[MCP] Every pooled MCP session is busy; agent {} calls {} on a per-call client",
                    agent_id,
                    _host(url),
                )
                return await MCPClient(url, api_key=key).call_tool(tool_name, arguments)
            return await entry.client.call_tool(tool_name, arguments)
        finally:
            if entry is not None:
                self._release(entry)
            await _close_clients(retired)

    def _acquire(self, key: MCPSessionKey, api_key: str | None, retired: list[_PooledSession]) -> _PooledSession | None:
        """Look up or insert without awaiting, mark the entry busy, and collect closable entries in ``retired``."""
        loop = asyncio.get_running_loop()
        now = self._clock()
        self._expire_idle(loop, now, retired)
        entry = self._entries.get(key)
        if entry is not None and entry.loop is not loop:
            # Its sockets belong to an earlier loop: never await their close from this one.
            del self._entries[key]
            entry = None
        if entry is None:
            if len(self._entries) >= self._max_entries and not self._evict_one(loop, retired):
                return None
            http = _mcp_http_client(_REQUEST_TIMEOUT_SECONDS, key.url)
            entry = _PooledSession(key, MCPClient(key.url, api_key, http=http), http, loop, now)
            self._entries[key] = entry
        else:
            self._entries.move_to_end(key)
        entry.in_use += 1
        entry.last_used = now
        return entry

    def _release(self, entry: _PooledSession) -> None:
        entry.in_use -= 1
        entry.last_used = self._clock()

    def _expire_idle(self, loop: asyncio.AbstractEventLoop, now: float, retired: list[_PooledSession]) -> None:
        for key, entry in list(self._entries.items()):
            if entry.in_use == 0 and now - entry.last_used > self._idle_ttl_seconds:
                del self._entries[key]
                if entry.loop is loop:
                    retired.append(entry)

    def _evict_one(self, loop: asyncio.AbstractEventLoop, retired: list[_PooledSession]) -> bool:
        """Drop the least recently used entry that is idle or bound to another loop; ``False`` if all are busy here."""
        victim = next(
            (key for key, entry in list(self._entries.items()) if entry.in_use == 0 or entry.loop is not loop), None
        )
        if victim is None:
            return False
        entry = self._entries.pop(victim)
        if entry.loop is loop:
            retired.append(entry)
        return True

    async def shutdown(self) -> None:
        """End and close every same-loop session within the shutdown bound; drop other loops' entries unawaited."""
        loop = asyncio.get_running_loop()
        owned = [entry for entry in self._entries.values() if entry.loop is loop]
        self._entries.clear()
        try:
            async with asyncio.timeout(self._shutdown_timeout_seconds):
                _ = await asyncio.gather(
                    *(_end_session(entry) for entry in owned if entry.client.session_id is not None)
                )
        except TimeoutError:
            logger.warning(
                "[MCP] Ending pooled MCP sessions took over {} s; closing their clients",
                self._shutdown_timeout_seconds,
            )
        await _close_clients(owned)


mcp_session_pool = MCPSessionPool()


async def shutdown_mcp_sessions() -> None:
    """End and close the pooled MCP sessions. App lifespan calls this."""
    await mcp_session_pool.shutdown()
