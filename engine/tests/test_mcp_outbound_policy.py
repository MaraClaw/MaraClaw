import json
from collections.abc import Callable
from contextlib import asynccontextmanager, nullcontext
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api import tools as tools_api
from app.core.outbound_policy import OutboundPolicyError
from app.core.security import get_current_user
from app.records.tool import ToolRecord
from app.records.user import UserRecord
from app.services import mcp_installation, resource_discovery
from app.services.agent_tool_exec.mcp_tools import _execute_mcp_tool
from app.services.mcp_client import MCPClient


def install_wire(
    monkeypatch: pytest.MonkeyPatch, respond: Callable[[httpx.Request], httpx.Response]
) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
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
    monkeypatch: pytest.MonkeyPatch, transport: str | None
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
    app.include_router(tools_api.router)
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
