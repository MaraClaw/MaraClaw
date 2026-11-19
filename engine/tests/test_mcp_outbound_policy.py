import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, nullcontext
from types import SimpleNamespace
from typing import override
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api import tools as tools_api
from app.core.outbound_policy import OutboundPolicyError
from app.core.outbound_transport import _PublicHTTPTransport
from app.core.security import get_current_user
from app.records.tool import ToolRecord
from app.records.user import UserRecord
from app.services import mcp_client, mcp_installation, resource_discovery
from app.services.agent_tool_exec.mcp_tools import _execute_mcp_tool
from app.services.mcp_client import (
    MCP_MAX_REDIRECT_BODY_BYTES,
    MCP_MAX_RESPONSE_BYTES,
    MCPClient,
    MCPResponseTooLargeError,
    MCPSessionPool,
    Transport,
)


def install_wire(
    monkeypatch: pytest.MonkeyPatch, respond: Callable[[httpx.Request], httpx.Response]
) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        _ = await request.aread()
        requests.append(request)
        return respond(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    return requests


@pytest.mark.parametrize("endpoint", ["messages?session=secret", "/rpc/messages?session=secret"])
async def test_sse_posts_handshake_and_request_to_same_origin(monkeypatch: pytest.MonkeyPatch, endpoint: str):
    # Given: a real MCP client and HTTP transport policy, with a simulated server below the policy seam.
    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text=f"event: endpoint\ndata: {endpoint}\n\n")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "lookup"}]}})

    requests = install_wire(monkeypatch, respond)
    client = MCPClient("https://8.8.8.8/rpc/sse", api_key="bearer-secret")
    client._transport = "sse"

    # When: discovering tools through the SSE transport.
    result = await client.list_tools()

    # Then: relative endpoints resolve against the SSE request path and credentials stay on that origin.
    assert result[0]["name"] == "lookup"
    assert [str(request.url) for request in requests[1:]] == ["https://8.8.8.8/rpc/messages?session=secret"] * 3
    assert all(request.headers["authorization"] == "Bearer bearer-secret" for request in requests)
    assert [json.loads(request.content)["method"] for request in requests[1:]] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
    ]


@pytest.mark.parametrize("connect_only", [False, True])
@pytest.mark.parametrize("endpoint", ["https://1.1.1.1/messages?session=secret", "//1.1.1.1/messages"])
async def test_sse_cross_origin_endpoint_is_blocked_before_post(
    monkeypatch: pytest.MonkeyPatch, connect_only: bool, endpoint: str
):
    # Given: an authenticated SSE server advertises a different origin.
    requests = install_wire(
        monkeypatch, lambda _request: httpx.Response(200, text=f"event: endpoint\ndata: {endpoint}\n\n")
    )
    client = MCPClient("https://8.8.8.8/rpc/sse", api_key="bearer-secret")
    operation = client._sse_connect() if connect_only else client._sse_request("tools/list")

    # When: either SSE path consumes its endpoint event.
    with pytest.raises(OutboundPolicyError):
        await operation

    # Then: no POST or credential-bearing cross-origin request reaches the wire.
    assert [(request.method, request.url.host) for request in requests] == [("GET", "8.8.8.8")]


@pytest.mark.parametrize("transport", [None, "streamable", "sse"])
async def test_cross_origin_redirect_is_blocked_without_protocol_fallback(
    monkeypatch: pytest.MonkeyPatch, transport: Transport | None
):
    # Given: every attempted MCP request redirects to a foreign destination.
    requests = install_wire(
        monkeypatch,
        lambda _request: httpx.Response(307, headers={"location": "https://1.1.1.1/stolen?token=secret"}),
    )
    client = MCPClient("https://8.8.8.8/mcp", api_key="bearer-secret")
    client._transport = transport
    client._session_id = "session-secret"

    # When: a real redirect is followed by HTTPX through the shared transport.
    with pytest.raises(OutboundPolicyError):
        await client.list_tools()

    # Then: no fallback or foreign request can leak authorization or MCP session state.
    assert len(requests) == 1
    assert requests[0].url.host == "8.8.8.8"
    assert client._transport == transport


async def test_initialization_policy_failure_is_not_swallowed(monkeypatch: pytest.MonkeyPatch):
    # Given: initialization itself redirects outside the configured origin.
    requests = install_wire(
        monkeypatch, lambda _request: httpx.Response(307, headers={"location": "https://1.1.1.1/init"})
    )
    client = MCPClient("https://8.8.8.8/mcp", api_key="secret")

    # When: the first discovery request initializes the transport.
    with pytest.raises(OutboundPolicyError):
        await client.list_tools()

    # Then: neither notification, tools/list, nor SSE fallback is attempted.
    assert len(requests) == 1
    assert json.loads(requests[0].content)["method"] == "initialize"


@pytest.mark.parametrize("url", ["http://127.0.0.1/mcp", "https://8.8.8.8/mcp"])
async def test_direct_import_never_persists_after_policy_failure(monkeypatch: pytest.MonkeyPatch, url: str):
    # Given: either preflight rejects the destination, or discovery redirects to another origin.
    requests = install_wire(
        monkeypatch, lambda _request: httpx.Response(307, headers={"location": "https://1.1.1.1/blocked"})
    )

    async def unexpected_persistence(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Policy failure must not query or persist a placeholder tool")

    for method in ("get_by_name", "create", "update_mcp_server_url"):
        monkeypatch.setattr(resource_discovery.tool_dao, method, unexpected_persistence)
    monkeypatch.setattr(resource_discovery.agent_tool_dao, "ensure_with_config", unexpected_persistence)

    # When: importing a direct server with credentials.
    result = await resource_discovery.import_mcp_direct(url, uuid4(), api_key="secret")

    # Then: the error is safe and persistence has not been attempted.
    assert "not allowed" in result
    assert "secret" not in result
    assert len(requests) == (0 if "127.0.0.1" in url else 1)


@pytest.mark.parametrize("url", ["http://127.0.0.1/mcp?secret=hidden", "https://8.8.8.8/mcp?secret=hidden"])
async def test_api_blocks_preflight_and_mid_discovery_policy_failures(monkeypatch: pytest.MonkeyPatch, url: str):
    # Given: the actual route stack with an authenticated member and a blocked destination or redirect.
    install_wire(monkeypatch, lambda _request: httpx.Response(307, headers={"location": "https://1.1.1.1/mcp"}))
    app = FastAPI()
    app.include_router(tools_api.probe_router)
    app.dependency_overrides[get_current_user] = lambda: UserRecord(id=uuid4())

    # When: testing the MCP connection through HTTP.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/tools/test-mcp", json={"server_url": url, "api_key": "secret"})

    # Then: policy failures retain the 400 contract without exposing secrets.
    assert response.status_code == 400
    assert response.json() == {"detail": "MCP server URL is not allowed"}


async def test_streamable_handshake_and_call_keep_credentials_on_origin(monkeypatch: pytest.MonkeyPatch):
    # Given: a streamable server issues a session during initialization.
    requests = install_wire(
        monkeypatch,
        lambda _request: httpx.Response(
            200,
            headers={"mcp-session-id": "session-secret"},
            json={"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "completed"}]}},
        ),
    )
    client = MCPClient("https://8.8.8.8/mcp?apiKey=bearer-secret")

    # When: executing a tool through the public client API.
    result = await client.call_tool("lookup", {"query": "example"})

    # Then: handshake and execution succeed with the original authentication contract.
    assert result == "completed"
    assert len(requests) == 3
    assert all(str(request.url) == "https://8.8.8.8/mcp" for request in requests)
    assert all(request.headers["authorization"] == "Bearer bearer-secret" for request in requests)
    assert requests[-1].headers["mcp-session-id"] == "session-secret"


async def test_sse_policy_failure_after_ordinary_streamable_failure_propagates(monkeypatch: pytest.MonkeyPatch):
    # Given: an ordinary protocol failure permits SSE detection, whose endpoint violates policy.
    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="event: endpoint\ndata: //1.1.1.1/messages\n\n")
        return httpx.Response(405)

    requests = install_wire(monkeypatch, respond)
    client = MCPClient("https://8.8.8.8/mcp")

    # When: auto-detection reaches the SSE endpoint.
    with pytest.raises(OutboundPolicyError):
        await client.list_tools()

    # Then: the policy error is not wrapped as an ordinary combined protocol failure.
    assert requests[-1].method == "GET"
    assert all(request.url.host == "8.8.8.8" for request in requests)


async def test_sse_connect_resolves_relative_endpoint_against_request_path(monkeypatch: pytest.MonkeyPatch):
    # Given: the separate connect path receives a path-relative endpoint.
    install_wire(monkeypatch, lambda _request: httpx.Response(200, text="event: endpoint\ndata: messages\n\n"))
    client = MCPClient("https://8.8.8.8/rpc/sse")

    # When: connecting without sending a tool request.
    endpoint = await client._sse_connect()

    # Then: the original SSE request URL supplies the base path.
    assert endpoint == "https://8.8.8.8/rpc/messages"


async def test_direct_import_preserves_placeholder_on_ordinary_protocol_failure(monkeypatch: pytest.MonkeyPatch):
    # Given: a public destination does not support either MCP protocol.
    install_wire(monkeypatch, lambda _request: httpx.Response(405))
    writes: list[str] = []

    async def lookup(_name: str) -> None:
        return None

    async def create(*_args: object, **_kwargs: object) -> ToolRecord:
        writes.append("tool")
        return ToolRecord(id=uuid4(), name="mcp_example")

    async def assign(*_args: object, **_kwargs: object) -> None:
        writes.append("assignment")

    @asynccontextmanager
    async def mocked_connection():
        yield SimpleNamespace(raw=SimpleNamespace(transaction=nullcontext))

    monkeypatch.setattr(mcp_installation, "connection_ctx", mocked_connection)
    monkeypatch.setattr(resource_discovery.tool_dao, "get_by_name", lookup)
    monkeypatch.setattr(resource_discovery.tool_dao, "create", create)
    monkeypatch.setattr(resource_discovery.agent_tool_dao, "ensure_with_config", assign)

    # When: importing the server despite an ordinary list failure.
    result = await resource_discovery.import_mcp_direct("https://8.8.8.8/mcp", uuid4(), "example")

    # Then: the existing placeholder and assignment behavior is retained.
    assert writes == ["tool", "assignment"]
    assert "Imported MCP server" in result


async def test_execution_returns_safe_error_for_stored_cross_origin_redirect(monkeypatch: pytest.MonkeyPatch):
    # Given: a persisted server attempts a credential-bearing cross-origin redirect during execution.
    requests = install_wire(
        monkeypatch,
        lambda _request: httpx.Response(307, headers={"location": "https://1.1.1.1/mcp?token=hidden-secret"}),
    )

    async def lookup(_name: str) -> ToolRecord:
        return ToolRecord(
            id=uuid4(), name="mcp_example", type="mcp", mcp_server_url="https://8.8.8.8/mcp?apiKey=hidden-secret"
        )

    monkeypatch.setattr(resource_discovery.tool_dao, "get_by_name", lookup)

    # When: the execution boundary invokes the actual MCP client.
    result = await _execute_mcp_tool("mcp_example", {})

    # Then: neither the destination URL nor its secrets escape in the returned error.
    assert "not allowed" in result
    assert "hidden-secret" not in result
    assert "https://" not in result
    assert len(requests) == 1


def streamable_reply(request: httpx.Request) -> httpx.Response:
    if request.method == "DELETE":
        return httpx.Response(200)
    return httpx.Response(
        200,
        headers={"mcp-session-id": "session-secret"},
        json={"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "completed"}]}},
    )


async def pooled_call(pool: MCPSessionPool, agent_id: UUID, server_url: str = "https://8.8.8.8/mcp") -> str:
    return await pool.call_tool(
        agent_id=agent_id, server_url=server_url, api_key=None, tool_name="lookup", arguments={}
    )


async def test_pooled_session_client_comes_from_public_http_client_for_the_normalized_origin(
    monkeypatch: pytest.MonkeyPatch,
):
    # Given: the real public transport below a spy on the pool's client factory.
    requests = install_wire(monkeypatch, streamable_reply)
    built: list[tuple[float, bool, str | None]] = []
    real_factory = mcp_client.public_http_client

    def spy(*, timeout: float, follow_redirects: bool = False, origin: str | None = None) -> httpx.AsyncClient:
        built.append((timeout, follow_redirects, origin))
        return real_factory(timeout=timeout, follow_redirects=follow_redirects, origin=origin)

    monkeypatch.setattr(mcp_client, "public_http_client", spy)
    pool = MCPSessionPool()
    agent_id = uuid4()
    try:
        # When: one agent calls twice through a URL carrying its key and a trailing slash.
        for _ in range(2):
            assert await pooled_call(pool, agent_id, "https://8.8.8.8/mcp/?apiKey=bearer-secret") == "completed"
        [entry] = pool._entries.values()
        transport = entry.http._transport
    finally:
        await pool.shutdown()

    # Then: one pinned public client bound to the normalized origin carried every request, the DELETE included.
    assert built == [(30, True, "https://8.8.8.8/mcp")]
    assert isinstance(transport, _PublicHTTPTransport)
    assert [request.method for request in requests] == ["POST", "POST", "POST", "POST", "DELETE"]
    assert all(str(request.url) == "https://8.8.8.8/mcp" for request in requests)
    assert all(request.headers["authorization"] == "Bearer bearer-secret" for request in requests)
    assert requests[-1].headers["mcp-session-id"] == "session-secret"


async def test_pooled_session_checks_the_origin_on_every_request(monkeypatch: pytest.MonkeyPatch):
    # Given: an established pooled session whose server later redirects to another origin.
    redirect: list[bool] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if redirect:
            return httpx.Response(307, headers={"location": "https://1.1.1.1/stolen?token=secret"})
        return streamable_reply(request)

    requests = install_wire(monkeypatch, respond)
    pool = MCPSessionPool()
    agent_id = uuid4()
    try:
        assert await pooled_call(pool, agent_id) == "completed"
        redirect.append(True)

        # When / Then: the reused client still refuses the foreign hop, and the policy error is not wrapped.
        with pytest.raises(OutboundPolicyError):
            await pooled_call(pool, agent_id)
    finally:
        redirect.clear()
        await pool.shutdown()
    assert [request.url.host for request in requests] == ["8.8.8.8"] * 5


def install_stored_agent_tool(monkeypatch: pytest.MonkeyPatch) -> MCPSessionPool:
    """Store ``mcp_example`` at https://8.8.8.8/mcp with an embedded key and route agent calls to a fresh pool."""

    async def lookup(_name: str) -> ToolRecord:
        return ToolRecord(
            id=uuid4(), name="mcp_example", type="mcp", mcp_server_url="https://8.8.8.8/mcp?apiKey=hidden-secret"
        )

    async def no_assignment(_agent_id: UUID, _tool_id: UUID) -> None:
        return None

    monkeypatch.setattr(resource_discovery.tool_dao, "get_by_name", lookup)
    monkeypatch.setattr(resource_discovery.agent_tool_dao, "get_assignment", no_assignment)
    pool = MCPSessionPool()
    monkeypatch.setattr(mcp_client, "mcp_session_pool", pool)
    return pool


async def test_pooled_initialize_policy_failure_returns_the_blocked_message(monkeypatch: pytest.MonkeyPatch):
    # Given: an agent's stored server redirects its very first initialize to another origin.
    requests = install_wire(
        monkeypatch,
        lambda _request: httpx.Response(307, headers={"location": "https://1.1.1.1/mcp?token=hidden-secret"}),
    )
    pool = install_stored_agent_tool(monkeypatch)

    # When: the agent executes the tool through the pooled path.
    try:
        result = await _execute_mcp_tool("mcp_example", {}, uuid4())
    finally:
        await pool.shutdown()

    # Then: the existing blocked message comes back and nothing else was sent.
    assert result == "MCP tool execution blocked: server destination is not allowed."
    assert len(requests) == 1
    assert json.loads(requests[0].content)["method"] == "initialize"


class CountingStream(httpx.AsyncByteStream):
    """A body of ``count`` copies of ``chunk`` after an optional ``first`` chunk; ``sent`` counts the copies read."""

    def __init__(self, chunk: bytes, count: int, first: bytes = b"") -> None:
        self.chunk: bytes = chunk
        self.count: int = count
        self.first: bytes = first
        self.sent: int = 0

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self.first:
            yield self.first
        for _ in range(self.count):
            self.sent += 1
            yield self.chunk


@pytest.mark.parametrize("declared", [True, False], ids=["content-length", "chunked"])
async def test_oversized_streamable_response_fails_before_parsing(monkeypatch: pytest.MonkeyPatch, declared: bool):
    # Given: a 1 KiB cap and a tools/call reply of 50 KiB, announced by Content-Length or not.
    monkeypatch.setattr(mcp_client, "MCP_MAX_RESPONSE_BYTES", 1024)
    parsed: list[int] = []
    real_loads = mcp_client.json_loads_value

    def recording_loads(value: str | bytes | bytearray) -> object:
        parsed.append(len(value))
        return real_loads(value)

    monkeypatch.setattr(mcp_client, "json_loads_value", recording_loads)
    bodies: list[CountingStream] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["method"] != "tools/call":
            return httpx.Response(200, headers={"mcp-session-id": "session"}, json={"result": {}})
        body = CountingStream(b"x" * 512, 100, first=b'{"result": "')
        bodies.append(body)
        headers = {"content-type": "application/json"}
        if declared:
            headers["content-length"] = str(512 * 100 + 12)
        return httpx.Response(200, headers=headers, stream=body)

    install_wire(monkeypatch, respond)
    client = MCPClient("https://8.8.8.8/mcp")

    # When: the transport reads the reply, and when the public call_tool does.
    with pytest.raises(MCPResponseTooLargeError):
        await client._streamable_request("tools/call", {"name": "lookup", "arguments": {}})
    result = await client.call_tool("lookup", {})

    # Then: a declared size is refused unread, a chunked body stops just past the cap, and neither is parsed.
    assert result.startswith("❌")
    assert [body.sent for body in bodies] == ([0, 0] if declared else [2, 2])
    assert parsed
    assert all(size < 1024 for size in parsed)


@pytest.mark.parametrize("connect_only", [False, True])
async def test_oversized_sse_stream_fails_before_parsing(monkeypatch: pytest.MonkeyPatch, connect_only: bool):
    # Given: a 1 KiB cap and an SSE stream that floods one endless line, before or after its endpoint event.
    monkeypatch.setattr(mcp_client, "MCP_MAX_RESPONSE_BYTES", 1024)
    streams: list[CountingStream] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            endpoint = b"" if connect_only else b"event: endpoint\ndata: /messages?session=1\n\nevent: message\n"
            stream = CountingStream(b"data: " + b"x" * 506, 100, first=endpoint)
            streams.append(stream)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)
        return httpx.Response(202)

    install_wire(monkeypatch, respond)
    client = MCPClient("https://8.8.8.8/sse")
    operation = (
        client._sse_connect()
        if connect_only
        else client._sse_request("tools/call", {"name": "lookup", "arguments": {}})
    )

    # When / Then: reading stops with the size error shortly after the cap instead of buffering the stream.
    with pytest.raises(MCPResponseTooLargeError):
        await operation
    assert [stream.sent for stream in streams] == [3]


class ChunkedStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks: list[bytes] = chunks

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk


@pytest.mark.parametrize(
    "text",
    [
        "event: endpoint\r\ndata: /messages\r\n\r\n",
        "data: a\rdata: b\ndata: c",
        "a\u2028b\x85c\x0cd\x1ce",
        "ends with cr\r",
        "x\n\r",
        "\r\n\r\n",
        "data: héllo wörld 😀\n\n",
    ],
)
async def test_sse_lines_split_like_splitlines_at_every_chunk_size(text: str):
    # Given: a stream body, cut into chunks of every size so breaks, "\r\n" pairs and UTF-8 sequences straddle them.
    body = text.encode()
    for size in range(1, len(body) + 1):
        stream = ChunkedStream([body[start : start + size] for start in range(0, len(body), size)])
        response = httpx.Response(200, stream=stream)

        # When: the capped reader yields its lines.
        lines = [line async for line in mcp_client._capped_lines(response, mcp_client._ByteBudget(len(body)))]

        # Then: they are exactly str.splitlines of the whole text.
        assert lines == text.splitlines(), size


def rpc_or_verb(request: httpx.Request) -> str:
    return str(json.loads(request.content)["method"]) if request.content else request.method


@pytest.mark.parametrize("declared", [True, False], ids=["content-length", "chunked"])
async def test_oversized_redirect_body_is_refused_before_httpx_buffers_it(
    monkeypatch: pytest.MonkeyPatch, declared: bool
):
    # Given: /mcp answers with a same-origin 307 carrying a 4 MiB body, announced by Content-Length or not.
    chunk = 1024
    bodies: list[CountingStream] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/mcp":
            return streamable_reply(request)
        body = CountingStream(b"x" * chunk, MCP_MAX_RESPONSE_BYTES // chunk)
        bodies.append(body)
        headers = {"location": "/mcp/"}
        if declared:
            headers["content-length"] = str(MCP_MAX_RESPONSE_BYTES)
        return httpx.Response(307, headers=headers, stream=body)

    requests = install_wire(monkeypatch, respond)
    pool = MCPSessionPool()

    # When: a per-call client initializes through the redirect, and so does an agent's pooled call.
    with pytest.raises(MCPResponseTooLargeError):
        await MCPClient("https://8.8.8.8/mcp")._streamable_request("tools/call", {"name": "lookup", "arguments": {}})
    try:
        result = await pooled_call(pool, uuid4())
    finally:
        await pool.shutdown()

    # Then: a declared size is refused unread, a streamed one stops at the first chunk past the redirect cap,
    # and the redirect target is never requested.
    first_chunk_past_cap = MCP_MAX_REDIRECT_BODY_BYTES // chunk + 1
    assert result.startswith("❌")
    assert [body.sent for body in bodies] == ([0, 0] if declared else [first_chunk_past_cap] * 2)
    assert [(request.url.path, rpc_or_verb(request)) for request in requests] == [("/mcp", "initialize")] * 2


async def test_same_origin_redirect_keeps_the_request_body_and_session(monkeypatch: pytest.MonkeyPatch):
    # Given: a server mounted at /mcp/ answers the slash-stripped /mcp with a 307 and a short streamed body.
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/mcp":
            return httpx.Response(307, headers={"location": "/mcp/"}, stream=CountingStream(b"Moved to /mcp/", 1))
        return streamable_reply(request)

    requests = install_wire(monkeypatch, respond)
    pool = MCPSessionPool()
    agent_id = uuid4()
    try:
        # When: one agent calls the tool twice, then the pool shuts down.
        for _ in range(2):
            result = await pool.call_tool(
                agent_id=agent_id,
                server_url="https://8.8.8.8/mcp",
                api_key="bearer-secret",
                tool_name="lookup",
                arguments={"query": "MC-16"},
            )
            assert result == "completed"
    finally:
        await pool.shutdown()

    # Then: each request took exactly one hop to /mcp/ with its method, body, credential and session intact,
    # and the second call reused the session.
    hops = list(zip(requests[::2], requests[1::2], strict=True))
    assert [(sent.url.path, followed.url.path) for sent, followed in hops] == [("/mcp", "/mcp/")] * 5
    assert all(sent.method == followed.method and sent.content == followed.content for sent, followed in hops)
    assert all(followed.headers["authorization"] == "Bearer bearer-secret" for _, followed in hops)
    assert [rpc_or_verb(followed) for _, followed in hops] == [
        "initialize",
        "notifications/initialized",
        "tools/call",
        "tools/call",
        "DELETE",
    ]
    assert [followed.headers.get("mcp-session-id") for _, followed in hops] == [None] + ["session-secret"] * 4
    assert json.loads(hops[2][1].content)["params"] == {"name": "lookup", "arguments": {"query": "MC-16"}}


async def test_cross_origin_hop_after_a_followed_redirect_returns_the_blocked_message(monkeypatch: pytest.MonkeyPatch):
    # Given: an agent's stored server redirects /mcp to /mcp/ on its origin, which then redirects to another origin.
    def respond(request: httpx.Request) -> httpx.Response:
        location = "/mcp/" if request.url.path == "/mcp" else "https://1.1.1.1/mcp/?token=hidden-secret"
        return httpx.Response(307, headers={"location": location}, stream=CountingStream(b"Moved", 1))

    requests = install_wire(monkeypatch, respond)
    pool = install_stored_agent_tool(monkeypatch)

    # When: the agent executes the tool through the pooled path.
    try:
        result = await _execute_mcp_tool("mcp_example", {}, uuid4())
    finally:
        await pool.shutdown()

    # Then: the same-origin hop is followed, the foreign hop never reaches the wire, and the blocked message returns.
    assert result == "MCP tool execution blocked: server destination is not allowed."
    assert [(request.url.host, request.url.path, rpc_or_verb(request)) for request in requests] == [
        ("8.8.8.8", "/mcp", "initialize"),
        ("8.8.8.8", "/mcp/", "initialize"),
    ]
