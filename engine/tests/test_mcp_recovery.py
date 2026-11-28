import asyncio
import inspect
import json
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Literal, override
from unittest.mock import AsyncMock

import httpx
import pytest

from app.core.json_types import JsonObject, JsonValue, is_json_object, json_loads_value
from app.services import agent_tools as agent_tools_module, mcp_client
from app.services.mcp_client import (
    MCP_MAX_TOOL_TEXT_CHARS,
    MCP_SESSION_IDLE_TTL_SECONDS,
    MCP_TOOL_CALL_DEADLINE_SECONDS,
    MCP_TOOL_DEADLINE_EXCEEDED_TEXT,
    MCP_TOOL_TEXT_TRUNCATION_MARKER,
    MCPClient,
    MCPClientError,
    MCPSessionPool,
    _StreamableUnsupportedError,
)
from loop_clock_fixtures import LoopClock, freeze_loop_clock

type Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]

AGENT = uuid.UUID(int=1)
URL = "https://mcp.example.test/mcp"
TOOL_RESULT = "ok"
HANDSHAKE = ["initialize", "notifications/initialized"]


def rpc_method(request: httpx.Request) -> str:
    if not request.content:
        return ""
    body = json_loads_value(request.content)
    method = body.get("method") if is_json_object(body) else None
    return method if isinstance(method, str) else ""


@dataclass(frozen=True)
class Seen:
    """One request as the fake MCP server received it."""

    http: str
    path: str
    rpc: str
    session: str | None


class FakeMCPServer:
    """MCP server behind a patched ``public_http_client``.

    Queued handlers answer one request each, keyed by JSON-RPC method or, without a body, by HTTP verb. Everything else
    gets the default streamable (or, with ``sse_only``, legacy SSE) behavior.
    """

    def __init__(self, *, sse_only: bool = False) -> None:
        self.sse_only: bool = sse_only
        self.seen: list[Seen] = []
        self.clients: list[httpx.AsyncClient] = []
        self.origins: list[str | None] = []
        self.delete_status: dict[str, int] = {}
        self._sessions: int = 0
        self._queued: defaultdict[str, list[Handler]] = defaultdict(list)

    def queue(self, key: str, *handlers: Handler) -> None:
        self._queued[key].extend(handlers)

    def factory(
        self, *, timeout: float, follow_redirects: bool = False, origin: str | None = None
    ) -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(self.handle), timeout=timeout, follow_redirects=follow_redirects
        )
        self.clients.append(client)
        self.origins.append(origin)
        return client

    def rpcs(self) -> list[str]:
        return [seen.rpc or seen.http for seen in self.seen]

    async def handle(self, request: httpx.Request) -> httpx.Response:
        rpc = rpc_method(request)
        self.seen.append(Seen(request.method, request.url.path, rpc, request.headers.get("mcp-session-id")))
        queued = self._queued[rpc or request.method]
        if queued:
            return await queued.pop(0)(request)
        return self.default(request)

    def default(self, request: httpx.Request) -> httpx.Response:
        rpc = rpc_method(request)
        if request.method == "DELETE":
            return httpx.Response(self.delete_status.get(request.headers.get("mcp-session-id", ""), 200))
        if self.sse_only and request.method == "GET":
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=b"event: endpoint\ndata: /mcp/messages?session=1\n\n",
            )
        if request.method != "POST" or (self.sse_only and request.url.path == "/mcp"):
            return httpx.Response(405)
        if rpc == "initialize" and not self.sse_only:
            self._sessions += 1
            return httpx.Response(
                200,
                headers={"mcp-session-id": f"s{self._sessions}"},
                json={"jsonrpc": "2.0", "id": 0, "result": {"protocolVersion": "2024-11-05"}},
            )
        if rpc == "tools/call":
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": TOOL_RESULT}]}}
            )
        return httpx.Response(202)


def reply(response: httpx.Response) -> Handler:
    async def send(_request: httpx.Request) -> httpx.Response:
        return response

    return send


def fail_with(error: type[httpx.TransportError]) -> Handler:
    async def send(request: httpx.Request) -> httpx.Response:
        raise error("simulated transport failure", request=request)

    return send


class Clock:
    def __init__(self) -> None:
        self.now: float = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> FakeMCPServer:
    fake = FakeMCPServer()
    monkeypatch.setattr(mcp_client, "public_http_client", fake.factory)
    return fake


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def pool(clock: Clock) -> AsyncIterator[MCPSessionPool]:
    sessions = MCPSessionPool(clock=clock)
    try:
        yield sessions
    finally:
        await sessions.shutdown()


@pytest.fixture
def log_lines(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    lines: list[str] = []

    def record(message: object, *args: object, **_kwargs: object) -> None:
        lines.append(f"{message} {args!r}")

    for level in ("debug", "info", "warning", "error", "exception"):
        monkeypatch.setattr(mcp_client.logger, level, record)
    return lines


async def call(sessions: MCPSessionPool, agent: uuid.UUID = AGENT, url: str = URL, key: str | None = "key-one") -> str:
    return await sessions.call_tool(
        agent_id=agent, server_url=url, api_key=key, tool_name="lookup", arguments={"query": "MC-16"}
    )


class FakeSmitheryResponse:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


class FakeSmitheryClient:
    def __init__(self, response: FakeSmitheryResponse):
        self.response = response
        self.calls: list[tuple[str, JsonObject, dict[str, str]]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback) -> bool:
        return False

    async def post(self, url: str, *, json: JsonObject, headers: dict[str, str]) -> FakeSmitheryResponse:
        self.calls.append((url, json, headers))
        return self.response


def _install_fake_smithery_http(monkeypatch, response: FakeSmitheryResponse):
    client = FakeSmitheryClient(response)
    client_settings = []

    def create_client(**settings):
        client_settings.append(settings)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", create_client)
    return client, client_settings


def _install_mcp_execution_db(monkeypatch, values):
    """Install DAO mocks for MCP execution.

    ``values`` is a queue of return values for successive DAO calls:
    get_by_name, optional get_mcp_by_mcp_tool_name, optional get_assignment.
    """
    from app.dao.tool_dao import agent_tool_dao, tool_dao

    queue = list(values)

    async def get_by_name(_name):
        if not queue:
            return None
        value = queue.pop(0)
        # Preserve legacy fallback: None means primary name miss.
        if value is None:
            return None
        if getattr(value, "type", None) is None and getattr(value, "mcp_server_url", None) is not None:
            value.type = "mcp"
        return value

    async def get_mcp_by_name(_name):
        if not queue:
            return None
        return queue.pop(0)

    async def get_assignment(_agent_id, _tool_id):
        if not queue:
            return None
        return queue.pop(0)

    monkeypatch.setattr(tool_dao, "get_by_name", get_by_name)
    monkeypatch.setattr(tool_dao, "get_mcp_by_mcp_tool_name", get_mcp_by_name)
    monkeypatch.setattr(agent_tool_dao, "get_assignment", get_assignment)


def _install_fake_pool(monkeypatch, result: str) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    class FakePool:
        async def call_tool(
            self,
            *,
            agent_id: uuid.UUID,
            server_url: str,
            api_key: str | None,
            tool_name: str,
            arguments: Mapping[str, JsonValue],
        ) -> str:
            calls.append(
                {
                    "agent_id": agent_id,
                    "server_url": server_url,
                    "api_key": api_key,
                    "tool_name": tool_name,
                    "arguments": arguments,
                }
            )
            return result

    monkeypatch.setattr(mcp_client, "mcp_session_pool", FakePool())
    return calls


@pytest.mark.asyncio
async def test_mcp_transport_error_keeps_streamable_failure_message(monkeypatch):
    client = MCPClient("https://example.test/mcp")

    async def fail_streamable(_method, _params=None):
        raise RuntimeError("streamable returned 401")

    async def fail_sse(_method, _params=None):
        raise RuntimeError("sse endpoint returned 404")

    monkeypatch.setattr(client, "_streamable_request", fail_streamable)
    monkeypatch.setattr(client, "_sse_request", fail_sse)

    with pytest.raises(Exception, match="Both transports failed") as exc_info:
        await client._detect_and_request("tools/list")

    message = str(exc_info.value)
    assert "Streamable HTTP: streamable returned 401" in message
    assert "SSE: sse endpoint returned 404" in message


@pytest.mark.asyncio
async def test_mcp_transport_caches_streamable_success(monkeypatch):
    client = MCPClient("https://example.test/mcp")
    calls = []

    async def streamable(method, params=None):
        calls.append((method, params))
        return {"result": {}}

    async def unexpected_sse(_method, _params=None):
        raise AssertionError("SSE should not be selected")

    monkeypatch.setattr(client, "_streamable_request", streamable)
    monkeypatch.setattr(client, "_sse_request", unexpected_sse)

    await client._detect_and_request("tools/list")
    await client._detect_and_request("tools/call", {"name": "ping"})

    assert client._transport == "streamable"
    assert calls == [("tools/list", None), ("tools/call", {"name": "ping"})]


@pytest.mark.asyncio
async def test_discovery_falls_back_to_sse_without_pinning_it(monkeypatch):
    # Given: an ordinary streamable failure (not a refused initialize) during idempotent discovery.
    client = MCPClient("https://example.test/mcp")
    sse_calls = []

    async def fail_streamable(_method, _params=None):
        raise RuntimeError("streamable unavailable")

    async def sse(method, params=None):
        sse_calls.append((method, params))
        return {"result": {}}

    monkeypatch.setattr(client, "_streamable_request", fail_streamable)
    monkeypatch.setattr(client, "_sse_request", sse)

    # When / Then: tools/list is served over SSE, but the transient failure pins nothing.
    assert await client._detect_and_request("tools/list") == {"result": {}}
    assert sse_calls == [("tools/list", None)]
    assert client._transport is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [MCPClientError("HTTP 503"), httpx.ReadTimeout("read timed out")])
async def test_tool_call_is_never_replayed_over_sse_after_ordinary_failure(monkeypatch, failure: Exception):
    # Given: the streamable tools/call fails after its POST may have reached the server.
    client = MCPClient("https://example.test/mcp")

    async def fail_streamable(_method, _params=None):
        raise failure

    async def unexpected_sse(_method, _params=None):
        raise AssertionError("tools/call must not be replayed over SSE")

    monkeypatch.setattr(client, "_streamable_request", fail_streamable)
    monkeypatch.setattr(client, "_sse_request", unexpected_sse)

    # When / Then: the original failure surfaces and the transport stays undetected.
    with pytest.raises(type(failure)):
        await client._detect_and_request("tools/call", {"name": "ping"})
    assert client._transport is None


@pytest.mark.asyncio
async def test_refused_streamable_initialize_falls_back_to_and_caches_sse(monkeypatch):
    # Given: the server refuses the streamable initialize, so no request has executed yet.
    client = MCPClient("https://example.test/mcp")
    sse_calls = []
    streamable_calls = []

    async def refuse_streamable(method, _params=None):
        streamable_calls.append(method)
        raise _StreamableUnsupportedError("HTTP 405")

    async def sse(method, params=None):
        sse_calls.append((method, params))
        return {"result": {}}

    monkeypatch.setattr(client, "_streamable_request", refuse_streamable)
    monkeypatch.setattr(client, "_sse_request", sse)

    # When: a tool call detects the transport and a second call follows.
    await client._detect_and_request("tools/call", {"name": "ping"})
    await client._detect_and_request("tools/call", {"name": "pong"})

    # Then: SSE is pinned and the second call skips the streamable attempt.
    assert client._transport == "sse"
    assert streamable_calls == ["tools/call"]
    assert sse_calls == [("tools/call", {"name": "ping"}), ("tools/call", {"name": "pong"})]


@pytest.mark.asyncio
async def test_extracted_mcp_tool_falls_back_and_merges_agent_config(monkeypatch):
    from app.services.agent_tool_exec import mcp_tools

    agent_id = uuid.uuid4()
    tool = SimpleNamespace(
        id=uuid.uuid4(),
        mcp_server_url="https://mcp.example.test",
        mcp_server_name="Example",
        mcp_tool_name="remote_name",
        config={"api_key": "global-key", "atlassian_api_key": "secondary-key"},
    )
    assignment = SimpleNamespace(config={"api_key": "agent-key"})
    _install_mcp_execution_db(monkeypatch, [None, tool, assignment])
    decrypted = []
    pool_calls = _install_fake_pool(monkeypatch, "executed")

    def decrypt(config):
        decrypted.append(config)
        return config

    monkeypatch.setattr(agent_tools_module, "_decrypt_sensitive_fields", decrypt)

    result = await mcp_tools._execute_mcp_tool("remote_name", {"issue": "MC-16"}, agent_id)

    assert result == "executed"
    assert decrypted == [{"api_key": "agent-key", "atlassian_api_key": "secondary-key"}]
    assert pool_calls == [
        {
            "agent_id": agent_id,
            "server_url": "https://mcp.example.test",
            "api_key": "agent-key",
            "tool_name": "remote_name",
            "arguments": {"issue": "MC-16"},
        }
    ]


@pytest.mark.asyncio
async def test_extracted_mcp_tool_returns_missing_server_url(monkeypatch):
    from app.services.agent_tool_exec import mcp_tools

    tool = SimpleNamespace(mcp_server_url=None)
    _install_mcp_execution_db(monkeypatch, [tool])

    result = await mcp_tools._execute_mcp_tool("missing_url", {})

    assert result == "❌ MCP tool missing_url has no server URL configured"


@pytest.mark.asyncio
async def test_extracted_mcp_tool_routes_smithery_server_with_config(monkeypatch):
    from app.services.agent_tool_exec import mcp_smithery, mcp_tools

    tool = SimpleNamespace(
        id=uuid.uuid4(),
        mcp_server_url="https://example.run.tools/mcp",
        mcp_server_name="Smithery",
        mcp_tool_name="remote_name",
        config={"smithery_namespace": "team", "smithery_connection_id": "connection"},
    )
    _install_mcp_execution_db(monkeypatch, [tool])
    calls = []

    async def execute_via_smithery(mcp_url, tool_name, arguments, config, agent_id=None):
        calls.append((mcp_url, tool_name, arguments, config, agent_id))
        return "smithery executed"

    monkeypatch.setattr(mcp_smithery, "_execute_via_smithery_connect", execute_via_smithery)

    result = await mcp_tools._execute_mcp_tool("configured_name", {"issue": "MC-16"})

    assert result == "smithery executed"
    assert calls == [
        (
            "https://example.run.tools/mcp",
            "remote_name",
            {"issue": "MC-16"},
            {"smithery_namespace": "team", "smithery_connection_id": "connection"},
            None,
        )
    ]


@pytest.mark.asyncio
async def test_extracted_mcp_tool_uses_atlassian_key_for_valid_agent(monkeypatch):
    from app.api import atlassian
    from app.services.agent_tool_exec import mcp_tools

    agent_id = uuid.uuid4()
    tool = SimpleNamespace(
        id=uuid.uuid4(),
        mcp_server_url="https://mcp.atlassian.example.test",
        mcp_server_name="Atlassian Rovo",
        mcp_tool_name="search_issues",
        config={},
    )
    _install_mcp_execution_db(monkeypatch, [tool, None])
    pool_calls = _install_fake_pool(monkeypatch, "atlassian executed")
    key_requests = []

    async def get_key(received_agent_id):
        key_requests.append(received_agent_id)
        return "channel-key"

    monkeypatch.setattr(atlassian, "get_atlassian_api_key_for_agent", get_key)

    result = await mcp_tools._execute_mcp_tool("configured_name", {"query": "MC-16"}, agent_id)

    assert result == "atlassian executed"
    assert key_requests == [agent_id]
    assert pool_calls == [
        {
            "agent_id": agent_id,
            "server_url": "https://mcp.atlassian.example.test",
            "api_key": "channel-key",
            "tool_name": "search_issues",
            "arguments": {"query": "MC-16"},
        }
    ]
    assert inspect.signature(mcp_tools._execute_mcp_tool).parameters["agent_id"].annotation == uuid.UUID | None


@pytest.mark.asyncio
async def test_smithery_recovery_does_not_store_auth_required_connection(monkeypatch):
    async def fake_ensure_connection(_api_key, _mcp_url, _display_name):
        return {
            "namespace": "shadowsseven",
            "connection_id": "new-auth-required",
            "auth_url": "https://smithery.run/shadowsseven/new-auth-required/setup",
        }

    monkeypatch.setattr(
        "app.services.resource_discovery._ensure_smithery_connection",
        fake_ensure_connection,
    )

    result = await agent_tools_module._smithery_auto_recover(
        "smithery-key",
        "https://twitter.run.tools",
        "shadowsseven",
        "old-working-connection",
        agent_id=uuid.uuid4(),
    )

    assert result is not None
    assert "Re-authorization needed" in result
    assert "https://smithery.run/shadowsseven/new-auth-required/setup" in result


@pytest.mark.asyncio
async def test_smithery_connect_recovers_on_auth_status_with_pinned_request(monkeypatch):
    from app.services import resource_discovery
    from app.services.agent_tool_exec import mcp_smithery

    async def get_api_key(_agent_id):
        return "smithery-key"

    async def recover(*args):
        return f"recovered:{args[3]}"

    client, client_settings = _install_fake_smithery_http(monkeypatch, FakeSmitheryResponse(401, "{}"))
    monkeypatch.setattr(resource_discovery, "_get_smithery_api_key", get_api_key)
    monkeypatch.setattr(mcp_smithery, "_smithery_auto_recover", recover)

    agent_id = uuid.uuid4()
    result = await mcp_smithery._execute_via_smithery_connect(
        "https://example.run.tools/mcp",
        "fetch_issue",
        {"issue": "MC-16"},
        {"smithery_namespace": "team", "smithery_connection_id": "connection"},
        agent_id,
    )

    assert result == "recovered:connection"
    assert client_settings == [{"timeout": 30, "follow_redirects": True}]
    assert client.calls == [
        (
            "https://api.smithery.ai/connect/team/connection/mcp",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "fetch_issue", "arguments": {"issue": "MC-16"}},
            },
            {
                "Authorization": "Bearer smithery-key",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
    ]


@pytest.mark.asyncio
async def test_smithery_connect_recovers_on_auth_json_rpc_error(monkeypatch):
    from app.services import resource_discovery
    from app.services.agent_tool_exec import mcp_smithery

    async def get_api_key(_agent_id):
        return "smithery-key"

    recover_calls = []

    async def recover(*args):
        recover_calls.append(args)
        return "reauthorize"

    _install_fake_smithery_http(
        monkeypatch,
        FakeSmitheryResponse(200, '{"error": {"message": "connection expired"}}'),
    )
    monkeypatch.setattr(resource_discovery, "_get_smithery_api_key", get_api_key)
    monkeypatch.setattr(mcp_smithery, "_smithery_auto_recover", recover)

    result = await mcp_smithery._execute_via_smithery_connect(
        "https://example.run.tools/mcp",
        "fetch_issue",
        {},
        {"smithery_namespace": "team", "smithery_connection_id": "connection"},
    )

    assert result == "reauthorize"
    assert recover_calls == [("smithery-key", "https://example.run.tools/mcp", "team", "connection", None)]


@pytest.mark.asyncio
async def test_smithery_connect_parses_sse_content_blocks(monkeypatch):
    from app.services import resource_discovery
    from app.services.agent_tool_exec import mcp_smithery

    async def get_api_key(_agent_id):
        return "smithery-key"

    _install_fake_smithery_http(
        monkeypatch,
        FakeSmitheryResponse(
            200,
            'event: message\ndata: {"result": {"content": [{"type": "text", "text": "first"}, '
            '{"type": "image", "mimeType": "image/png"}, "third", {"kind": "other"}]}}',
        ),
    )
    monkeypatch.setattr(resource_discovery, "_get_smithery_api_key", get_api_key)

    result = await mcp_smithery._execute_via_smithery_connect(
        "https://example.run.tools/mcp",
        "fetch_issue",
        {},
        {"smithery_namespace": "team", "smithery_connection_id": "connection"},
    )

    assert result == "first\n[Image: image/png]\nthird\n{'kind': 'other'}"


@pytest.mark.asyncio
async def test_smithery_connect_parses_plain_json_and_preserves_errors(monkeypatch):
    from app.services import resource_discovery
    from app.services.agent_tool_exec import mcp_smithery

    async def get_api_key(_agent_id):
        return "smithery-key"

    monkeypatch.setattr(resource_discovery, "_get_smithery_api_key", get_api_key)
    for response, expected in (
        (FakeSmitheryResponse(200, '{"result": "plain result"}'), "plain result"),
        (FakeSmitheryResponse(200, '{"error": {"message": "bad request"}}'), "❌ MCP tool error: bad request"),
        (FakeSmitheryResponse(200, "not json"), "❌ Unexpected response from Smithery: not json"),
    ):
        _install_fake_smithery_http(monkeypatch, response)
        result = await mcp_smithery._execute_via_smithery_connect(
            "https://example.run.tools/mcp",
            "fetch_issue",
            {},
            {"smithery_namespace": "team", "smithery_connection_id": "connection"},
        )
        assert result == expected


# ── Pooled sessions ──────────────────────────────────────────


async def test_second_call_reuses_the_initialized_session(server: FakeMCPServer, pool: MCPSessionPool) -> None:
    # Given / When: one agent calls the same server twice.
    assert await call(pool) == TOOL_RESULT
    assert await call(pool) == TOOL_RESULT

    # Then: one handshake on one pooled client; the second call only sends tools/call with the first session.
    assert server.rpcs() == [*HANDSHAKE, "tools/call", "tools/call"]
    assert [seen.session for seen in server.seen] == [None, "s1", "s1", "s1"]
    assert server.origins == [URL]
    assert not server.clients[0].is_closed


async def test_sessions_are_isolated_by_agent_url_and_credential(server: FakeMCPServer, pool: MCPSessionPool) -> None:
    # Given / When: the same server is called by another agent, at another URL and with a rotated key.
    assert await call(pool) == TOOL_RESULT
    assert await call(pool, agent=uuid.UUID(int=2)) == TOOL_RESULT
    assert await call(pool, url="https://mcp.example.test/other") == TOOL_RESULT
    assert await call(pool, key="key-two") == TOOL_RESULT
    # An embedded ?apiKey= and a trailing slash normalize to the first call's URL and credential.
    assert await call(pool, url=f"{URL}/?apiKey=key-one", key=None) == TOOL_RESULT

    # Then: each distinct key got its own session and client; the normalized repeat reused the first.
    assert server.rpcs().count("initialize") == 4
    assert len(server.clients) == 4
    tool_calls = [seen for seen in server.seen if seen.rpc == "tools/call"]
    assert [seen.session for seen in tool_calls] == ["s1", "s2", "s3", "s4", "s1"]
    assert server.origins == [URL, URL, "https://mcp.example.test/other", URL]


async def test_concurrent_first_calls_initialize_once(
    server: FakeMCPServer, pool: MCPSessionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: an observable init lock and a server that holds the first initialize open.
    contended = asyncio.Event()

    class ObservedLock(asyncio.Lock):
        @override
        async def acquire(self) -> Literal[True]:
            if self.locked():
                contended.set()
            return await super().acquire()

    monkeypatch.setattr(asyncio, "Lock", ObservedLock)
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_initialize(request: httpx.Request) -> httpx.Response:
        entered.set()
        await release.wait()
        return server.default(request)

    server.queue("initialize", slow_initialize)

    # When: a second first call arrives while the first is still initializing.
    first = asyncio.create_task(call(pool))
    async with asyncio.timeout(5):
        await entered.wait()
        second = asyncio.create_task(call(pool))
        await contended.wait()
    release.set()

    # Then: the second call waited for the lock and reused the session instead of initializing again.
    assert await asyncio.gather(first, second) == [TOOL_RESULT, TOOL_RESULT]
    assert server.rpcs() == [*HANDSHAKE, "tools/call", "tools/call"]
    assert len(server.clients) == 1


async def test_expired_session_reinitializes_and_retries_once(server: FakeMCPServer, pool: MCPSessionPool) -> None:
    # Given: an established session that the server has since dropped.
    assert await call(pool) == TOOL_RESULT
    server.queue("tools/call", reply(httpx.Response(404)))

    # When
    assert await call(pool) == TOOL_RESULT

    # Then: the 404 dropped s1, one new handshake ran without it, and the call was retried once on s2.
    assert server.rpcs() == [*HANDSHAKE, "tools/call", "tools/call", *HANDSHAKE, "tools/call"]
    assert [seen.session for seen in server.seen] == [None, "s1", "s1", "s1", None, "s2", "s2"]


async def test_second_404_after_reinitializing_is_an_error(server: FakeMCPServer, pool: MCPSessionPool) -> None:
    server.queue("tools/call", reply(httpx.Response(404)), reply(httpx.Response(404)))

    result = await call(pool)

    assert result.startswith("❌")
    assert server.rpcs() == [*HANDSHAKE, "tools/call", *HANDSHAKE, "tools/call"]


@pytest.mark.parametrize(
    "failure",
    [reply(httpx.Response(503)), fail_with(httpx.ReadTimeout), fail_with(httpx.RemoteProtocolError)],
    ids=["503", "read-timeout", "protocol-error"],
)
async def test_tool_call_failure_after_the_post_is_reported_once(
    server: FakeMCPServer, pool: MCPSessionPool, failure: Handler
) -> None:
    # Given: a first call whose tools/call POST fails after it was sent.
    server.queue("tools/call", failure)

    # When
    result = await call(pool)

    # Then: one tools/call reached the server, nothing was retried or replayed over SSE.
    assert result.startswith("❌")
    assert server.rpcs() == [*HANDSHAKE, "tools/call"]
    assert all(seen.http == "POST" and seen.path == "/mcp" for seen in server.seen)

    # And: the failure pinned nothing; the session stays usable.
    assert await call(pool) == TOOL_RESULT
    assert server.rpcs()[3:] == ["tools/call"]


@pytest.mark.parametrize(
    ("status", "content_type"),
    [(404, None), (405, None), (406, None), (415, None), (200, "text/html")],
)
async def test_refused_initialize_pins_sse_for_the_pooled_session(
    monkeypatch: pytest.MonkeyPatch, pool: MCPSessionPool, status: int, content_type: str | None
) -> None:
    # Given: a legacy SSE server whose streamable endpoint refuses initialize.
    server = FakeMCPServer(sse_only=True)
    monkeypatch.setattr(mcp_client, "public_http_client", server.factory)
    headers = {"content-type": content_type} if content_type is not None else {}
    server.queue("initialize", reply(httpx.Response(status, headers=headers, text="no streamable here")))

    # When: the agent calls twice.
    assert await call(pool) == TOOL_RESULT
    assert await call(pool) == TOOL_RESULT

    # Then: only the first call tried streamable; both ran over SSE on the one pooled client.
    sse_call = [
        ("GET", "/mcp/sse", ""),
        ("POST", "/mcp/messages", "initialize"),
        ("POST", "/mcp/messages", "notifications/initialized"),
        ("POST", "/mcp/messages", "tools/call"),
    ]
    assert [(seen.http, seen.path, seen.rpc) for seen in server.seen] == [
        ("POST", "/mcp", "initialize"),
        *sse_call,
        *sse_call,
    ]
    assert len(server.clients) == 1


@pytest.mark.parametrize(
    "failure",
    [fail_with(httpx.ConnectError), fail_with(httpx.ReadTimeout), reply(httpx.Response(503))],
    ids=["connect-error", "read-timeout", "503"],
)
async def test_transient_initialize_failure_does_not_pin_sse(
    server: FakeMCPServer, pool: MCPSessionPool, failure: Handler
) -> None:
    # Given: the first initialize fails transiently.
    server.queue("initialize", failure)

    # When
    result = await call(pool)

    # Then: the call fails without trying SSE or sending tools/call, and the next call tries streamable again.
    assert result.startswith("❌")
    assert server.rpcs() == ["initialize"]
    assert await call(pool) == TOOL_RESULT
    assert server.rpcs() == ["initialize", *HANDSHAKE, "tools/call"]
    assert all(seen.http == "POST" and seen.path == "/mcp" for seen in server.seen)


async def test_session_bound_to_an_earlier_event_loop_is_discarded(server: FakeMCPServer, pool: MCPSessionPool) -> None:
    # Given: a session created on another event loop that has since closed.
    def call_in_another_loop() -> str:
        return asyncio.run(call(pool))

    assert await asyncio.to_thread(call_in_another_loop) == TOOL_RESULT

    # When: the same agent calls from the current loop.
    assert await call(pool) == TOOL_RESULT

    # Then: the stale entry was dropped without awaiting its close, and a fresh session replaced it.
    stale, fresh = server.clients
    assert not stale.is_closed
    assert [entry.http for entry in pool._entries.values()] == [fresh]
    assert server.rpcs() == [*HANDSHAKE, "tools/call"] * 2


async def test_idle_session_expires_after_the_ttl(server: FakeMCPServer, pool: MCPSessionPool, clock: Clock) -> None:
    # Given: a session idle for exactly the TTL is still reused.
    assert await call(pool) == TOOL_RESULT
    clock.now = MCP_SESSION_IDLE_TTL_SECONDS
    assert await call(pool) == TOOL_RESULT
    assert len(server.clients) == 1

    # When: it then stays idle for longer than the TTL.
    clock.now += MCP_SESSION_IDLE_TTL_SECONDS + 1
    assert await call(pool) == TOOL_RESULT

    # Then: the expired client was closed and a new session was initialized.
    expired, fresh = server.clients
    assert expired.is_closed
    assert not fresh.is_closed
    assert server.rpcs() == [*HANDSHAKE, "tools/call", "tools/call", *HANDSHAKE, "tools/call"]


async def test_full_pool_evicts_the_least_recently_used_idle_session(server: FakeMCPServer, clock: Clock) -> None:
    sessions = MCPSessionPool(max_entries=2, clock=clock)
    first, second, third = uuid.UUID(int=1), uuid.UUID(int=2), uuid.UUID(int=3)
    try:
        # Given: two pooled sessions, the first used most recently.
        for agent in (first, second, first):
            assert await call(sessions, agent=agent) == TOOL_RESULT

        # When: a third agent needs a session, and the first agent calls again.
        assert await call(sessions, agent=third) == TOOL_RESULT
        assert await call(sessions, agent=first) == TOOL_RESULT

        # Then: the least recently used idle session was evicted and closed; the others stayed pooled.
        assert [client.is_closed for client in server.clients] == [False, True, False]
        assert {key.agent_id for key in sessions._entries} == {first, third}
        assert server.rpcs().count("initialize") == 3
    finally:
        await sessions.shutdown()


async def test_full_pool_of_busy_sessions_serves_the_call_unpooled(server: FakeMCPServer, clock: Clock) -> None:
    sessions = MCPSessionPool(max_entries=1, clock=clock)
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_call(request: httpx.Request) -> httpx.Response:
        entered.set()
        await release.wait()
        return server.default(request)

    server.queue("tools/call", slow_call)
    try:
        # Given: the only pooled session is busy with a call.
        busy = asyncio.create_task(call(sessions))
        async with asyncio.timeout(5):
            await entered.wait()

        # When: another agent calls.
        assert await call(sessions, agent=uuid.UUID(int=2)) == TOOL_RESULT

        # Then: it ran on a per-call client closed after the call, without evicting the busy session.
        pooled, per_call = server.clients
        assert per_call.is_closed
        assert not pooled.is_closed
        assert {key.agent_id for key in sessions._entries} == {AGENT}
        release.set()
        assert await busy == TOOL_RESULT
    finally:
        release.set()
        await sessions.shutdown()


async def test_executor_calls_without_an_agent_use_per_call_clients(
    server: FakeMCPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.agent_tool_exec import mcp_tools

    tool = SimpleNamespace(
        id=uuid.uuid4(), mcp_server_url=URL, mcp_server_name="Example", mcp_tool_name="lookup", config={}
    )
    _install_mcp_execution_db(monkeypatch, [tool, tool])
    sessions = MCPSessionPool()
    monkeypatch.setattr(mcp_client, "mcp_session_pool", sessions)

    assert await mcp_tools._execute_mcp_tool("lookup", {}) == TOOL_RESULT
    assert await mcp_tools._execute_mcp_tool("lookup", {}) == TOOL_RESULT

    assert server.rpcs() == [*HANDSHAKE, "tools/call"] * 2
    assert [client.is_closed for client in server.clients] == [True, True]
    assert not sessions._entries


async def test_executor_calls_for_an_agent_share_one_pooled_session(
    server: FakeMCPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.agent_tool_exec import mcp_tools

    tool = SimpleNamespace(
        id=uuid.uuid4(), mcp_server_url=URL, mcp_server_name="Example", mcp_tool_name="lookup", config={}
    )
    _install_mcp_execution_db(monkeypatch, [tool, None, tool, None])
    sessions = MCPSessionPool()
    monkeypatch.setattr(mcp_client, "mcp_session_pool", sessions)
    try:
        assert await mcp_tools._execute_mcp_tool("lookup", {}, AGENT) == TOOL_RESULT
        assert await mcp_tools._execute_mcp_tool("lookup", {}, AGENT) == TOOL_RESULT
        assert server.rpcs() == [*HANDSHAKE, "tools/call", "tools/call"]
        assert {key.agent_id for key in sessions._entries} == {AGENT}
    finally:
        await sessions.shutdown()


async def test_raw_api_key_never_reaches_pool_keys_or_logs(
    server: FakeMCPServer, pool: MCPSessionPool, log_lines: list[str]
) -> None:
    secret = "raw-secret-key-value"
    # Given: a call that has to recover an expired session, then one that fails.
    server.queue("tools/call", reply(httpx.Response(404)))
    assert await call(pool, key=secret) == TOOL_RESULT
    server.queue("tools/call", reply(httpx.Response(503)))
    assert (await call(pool, key=secret)).startswith("❌")

    # Then: the pool key holds only a fingerprint of the key.
    [(key, entry)] = pool._entries.items()
    assert len(key.credential) == 32
    assert secret not in repr(key)
    assert secret not in repr(entry)

    # And: no log line carries the key, ending the session included.
    await pool.shutdown()
    assert log_lines
    assert not [line for line in log_lines if secret in line]


# ── Tool call deadline ───────────────────────────────────────

MAX_PINGS = 100
SSE_HEADERS = {"content-type": "text/event-stream"}
SSE_ENDPOINT_EVENT = b"event: endpoint\ndata: /mcp/messages?session=1\n\n"
SMITHERY_CONFIG = {"smithery_namespace": "team", "smithery_connection_id": "connection"}


class PingStream:
    """An event stream that answers every read with a keepalive ping and never ends.

    Each ping moves the frozen loop clock on by 'tick'; 'finished' is set when the reader lets go of the stream, and a
    deadline that never fires fails the test at MAX_PINGS instead of looping.
    """

    def __init__(self, clock: LoopClock, tick: float, head: bytes = b"") -> None:
        self.clock = clock
        self.tick = tick
        self.head = head
        self.pings = 0
        self.finished = asyncio.Event()

    async def body(self) -> AsyncIterator[bytes]:
        try:
            if self.head:
                yield self.head
            while True:
                assert self.pings < MAX_PINGS, "the tool call deadline never fired"
                await asyncio.sleep(0)
                self.clock.jump(self.tick)
                self.pings += 1
                yield b": ping\n\n"
        finally:
            self.finished.set()

    def handler(self) -> Handler:
        async def send(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers=SSE_HEADERS, content=self.body())

        return send


async def test_pings_cannot_keep_a_pooled_tool_call_alive_past_its_deadline(
    server: FakeMCPServer, pool: MCPSessionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a streamable server that answers tools/call with an event stream of nothing but pings.
    clock = freeze_loop_clock(monkeypatch)
    pings = PingStream(clock, MCP_TOOL_CALL_DEADLINE_SECONDS / 3)
    server.queue("tools/call", pings.handler())

    # When the agent calls the tool.
    result = await call(pool)

    # Then the call failed at its deadline and its stream was let go, with one tools/call sent and none replayed.
    assert result == MCP_TOOL_DEADLINE_EXCEEDED_TEXT
    assert pings.finished.is_set()
    assert server.rpcs() == [*HANDSHAKE, "tools/call"]

    # And the session stays usable.
    assert await call(pool) == TOOL_RESULT
    assert server.rpcs() == [*HANDSHAKE, "tools/call", "tools/call"]


async def test_pings_cannot_keep_an_unpooled_tool_call_alive_past_its_deadline(
    server: FakeMCPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = freeze_loop_clock(monkeypatch)
    pings = PingStream(clock, MCP_TOOL_CALL_DEADLINE_SECONDS / 3)
    server.queue("tools/call", pings.handler())

    result = await MCPClient(URL).call_tool("lookup", {})

    assert result == MCP_TOOL_DEADLINE_EXCEEDED_TEXT
    assert pings.finished.is_set()
    assert server.rpcs() == [*HANDSHAKE, "tools/call"]
    assert [client.is_closed for client in server.clients] == [True]


async def test_pings_cannot_keep_a_legacy_sse_tool_call_alive_past_its_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a legacy SSE server that accepts the call, then answers only with pings on its event stream.
    clock = freeze_loop_clock(monkeypatch)
    server = FakeMCPServer(sse_only=True)
    monkeypatch.setattr(mcp_client, "public_http_client", server.factory)
    pings = PingStream(clock, MCP_TOOL_CALL_DEADLINE_SECONDS / 3, head=SSE_ENDPOINT_EVENT)
    server.queue("GET", pings.handler())
    server.queue("tools/call", reply(httpx.Response(202)))

    # When the call is made.
    result = await MCPClient(URL).call_tool("lookup", {})

    # Then it failed at its deadline after one tools/call, which was not sent again.
    assert result == MCP_TOOL_DEADLINE_EXCEEDED_TEXT
    assert pings.finished.is_set()
    assert server.rpcs().count("tools/call") == 1


async def test_cancelling_a_tool_call_is_not_reported_as_a_deadline(
    server: FakeMCPServer, pool: MCPSessionPool
) -> None:
    entered = asyncio.Event()

    async def hang(_request: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.Event().wait()
        return httpx.Response(200)

    server.queue("tools/call", hang)
    calling = asyncio.create_task(call(pool))
    await entered.wait()

    _ = calling.cancel()

    with pytest.raises(asyncio.CancelledError):
        await calling


async def test_shutdown_stays_bounded_while_a_tool_call_is_still_in_flight(server: FakeMCPServer, clock: Clock) -> None:
    # Given a pooled session with a tool call parked on its server, and a server that never answers the DELETE.
    sessions = MCPSessionPool(shutdown_timeout_seconds=0.05, clock=clock)
    called, deleted = asyncio.Event(), asyncio.Event()

    async def hang_call(_request: httpx.Request) -> httpx.Response:
        called.set()
        await asyncio.Event().wait()
        return httpx.Response(200)

    async def hang_delete(_request: httpx.Request) -> httpx.Response:
        deleted.set()
        await asyncio.Event().wait()
        return httpx.Response(200)

    server.queue("tools/call", hang_call)
    server.queue("DELETE", hang_delete)
    calling = asyncio.create_task(call(sessions))
    await called.wait()
    try:
        # When shutdown runs under a guard far above its own bound.
        async with asyncio.timeout(5):
            await sessions.shutdown()

        # Then the DELETE was attempted and abandoned at the bound, and the pooled client still closed.
        assert deleted.is_set()
        assert server.clients[0].is_closed
    finally:
        _ = calling.cancel()
        with pytest.raises(asyncio.CancelledError):
            await calling


class HangingSmitheryClient(FakeSmitheryClient):
    def __init__(self) -> None:
        super().__init__(FakeSmitheryResponse(200, ""))
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()

    @override
    async def post(self, url: str, *, json: JsonObject, headers: dict[str, str]) -> FakeSmitheryResponse:
        self.calls.append((url, json, headers))
        self.entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.set()
        return self.response


async def smithery_call() -> str:
    from app.services.agent_tool_exec import mcp_smithery

    return await mcp_smithery._execute_via_smithery_connect(
        "https://example.run.tools/mcp", "fetch_issue", {}, dict(SMITHERY_CONFIG)
    )


@pytest.fixture
def smithery_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import resource_discovery

    async def get_api_key(_agent_id: uuid.UUID | None) -> str:
        return "smithery-key"

    monkeypatch.setattr(resource_discovery, "_get_smithery_api_key", get_api_key)


@pytest.mark.usefixtures("smithery_key")
async def test_smithery_connect_stops_at_the_tool_call_deadline_and_never_replays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.agent_tool_exec import mcp_smithery

    # Given a Smithery Connect request that never answers, a recovery that must not run, and a frozen loop clock.
    clock = freeze_loop_clock(monkeypatch)
    hanging = HangingSmitheryClient()
    recover = AsyncMock(return_value="recovered")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_settings: hanging)
    monkeypatch.setattr(mcp_smithery, "_smithery_auto_recover", recover)
    calling = asyncio.create_task(smithery_call())
    await hanging.entered.wait()

    # When the overall deadline passes.
    await clock.advance(MCP_TOOL_CALL_DEADLINE_SECONDS)

    # Then the call failed, its request was cancelled, and nothing was sent again or recovered.
    assert calling.done()
    assert calling.result() == MCP_TOOL_DEADLINE_EXCEEDED_TEXT
    assert hanging.cancelled.is_set()
    assert len(hanging.calls) == 1
    recover.assert_not_awaited()


@pytest.mark.usefixtures("smithery_key")
@pytest.mark.parametrize("extra", [0, 1, 5_000])
@pytest.mark.parametrize("shape", ["plain-result", "content-blocks"])
async def test_smithery_connect_caps_returned_text_like_other_mcp_results(
    monkeypatch: pytest.MonkeyPatch, shape: str, extra: int
) -> None:
    # Given Smithery text that is the cap plus 'extra' characters, as a plain result or as content blocks.
    text = "a" * (MCP_MAX_TOOL_TEXT_CHARS + extra)
    result: JsonValue = text if shape == "plain-result" else {"content": [{"type": "text", "text": text}]}
    _ = _install_fake_smithery_http(monkeypatch, FakeSmitheryResponse(200, json.dumps({"result": result})))

    # When the tool is called.
    returned = await smithery_call()

    # Then text within the cap is untouched; longer text keeps its first cap characters plus the marker line.
    assert returned == (text if extra == 0 else f"{text[:MCP_MAX_TOOL_TEXT_CHARS]}\n{MCP_TOOL_TEXT_TRUNCATION_MARKER}")
