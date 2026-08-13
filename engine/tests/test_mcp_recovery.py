import inspect
import uuid
from collections.abc import Mapping
from types import SimpleNamespace

import httpx
import pytest

from app.core.json_types import JsonObject, JsonValue
from app.services import agent_tools as agent_tools_module
from app.services.mcp_client import MCPClient


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


def _install_fake_mcp_client(monkeypatch, result: str):
    from app.services import mcp_client

    calls: list[tuple[str, str | None] | tuple[str, Mapping[str, JsonValue]]] = []

    class FakeMcpClient:
        def __init__(self, server_url: str, api_key: str | None = None):
            calls.append((server_url, api_key))

        async def call_tool(self, tool_name: str, arguments: Mapping[str, JsonValue]) -> str:
            calls.append((tool_name, arguments))
            return result

    monkeypatch.setattr(mcp_client, "MCPClient", FakeMcpClient)
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
async def test_mcp_transport_falls_back_to_and_caches_sse(monkeypatch):
    client = MCPClient("https://example.test/mcp")
    sse_calls = []

    async def fail_streamable(_method, _params=None):
        raise RuntimeError("streamable unavailable")

    async def sse(method, params=None):
        sse_calls.append((method, params))
        return {"result": {}}

    monkeypatch.setattr(client, "_streamable_request", fail_streamable)
    monkeypatch.setattr(client, "_sse_request", sse)

    await client._detect_and_request("tools/list")
    await client._detect_and_request("tools/call", {"name": "ping"})

    assert client._transport == "sse"
    assert sse_calls == [("tools/list", None), ("tools/call", {"name": "ping"})]


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
    client_calls = _install_fake_mcp_client(monkeypatch, "executed")

    def decrypt(config):
        decrypted.append(config)
        return config

    monkeypatch.setattr(agent_tools_module, "_decrypt_sensitive_fields", decrypt)

    result = await mcp_tools._execute_mcp_tool("remote_name", {"issue": "MC-16"}, agent_id)

    assert result == "executed"
    assert decrypted == [{"api_key": "agent-key", "atlassian_api_key": "secondary-key"}]
    assert client_calls == [
        ("https://mcp.example.test", "agent-key"),
        ("remote_name", {"issue": "MC-16"}),
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
    client_calls = _install_fake_mcp_client(monkeypatch, "atlassian executed")
    key_requests = []

    async def get_key(received_agent_id):
        key_requests.append(received_agent_id)
        return "channel-key"

    monkeypatch.setattr(atlassian, "get_atlassian_api_key_for_agent", get_key)

    result = await mcp_tools._execute_mcp_tool("configured_name", {"query": "MC-16"}, agent_id)

    assert result == "atlassian executed"
    assert key_requests == [agent_id]
    assert client_calls == [
        ("https://mcp.atlassian.example.test", "channel-key"),
        ("search_issues", {"query": "MC-16"}),
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

    def fail_if_db_touched():
        raise AssertionError("auth-required Smithery connections must not overwrite stored config")

    monkeypatch.setattr(
        "app.services.resource_discovery._ensure_smithery_connection",
        fake_ensure_connection,
    )
    monkeypatch.setattr(agent_tools_module, "async_session", fail_if_db_touched, raising=False)

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
