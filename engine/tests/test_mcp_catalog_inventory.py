import json
from collections.abc import AsyncIterator
from typing import override
from unittest.mock import AsyncMock

import anyio
import httpx
import pytest

from app.core.json_types import JsonObject
from app.services.mcp_client import MCPClient, MCPClientError
from test_mcp_outbound_policy import install_wire


async def test_complete_inventory_follows_opaque_cursors(monkeypatch: pytest.MonkeyPatch) -> None:
    request = AsyncMock(
        side_effect=[
            {"result": {"tools": [{"name": "first", "inputSchema": {"type": "object"}}], "nextCursor": "page+/=2"}},
            {"result": {"tools": [{"name": "last", "description": "Final", "inputSchema": {}}]}},
        ]
    )
    monkeypatch.setattr(MCPClient, "_detect_and_request", request)
    result = await MCPClient("https://mcp.example.com").list_tools()
    assert result == [
        {"name": "first", "description": "", "inputSchema": {"type": "object"}},
        {"name": "last", "description": "Final", "inputSchema": {}},
    ]
    assert request.await_args_list[1].args == ("tools/list", {"cursor": "page+/=2"})


@pytest.mark.parametrize(
    "page",
    [
        {},
        {"error": {"message": "secret upstream payload"}},
        {"result": {}},
        {"result": {"tools": "invalid"}},
        {"result": {"tools": [None]}},
        {"result": {"tools": [{"name": ""}]}},
        {"result": {"tools": [{"name": 42}]}},
        {"result": {"tools": [{"name": "bad", "inputSchema": []}]}},
        {"result": {"tools": [{"name": "bad", "inputSchema": {"type": "string"}}]}},
        {"result": {"tools": [], "nextCursor": 12}},
        {"result": {"tools": [], "nextCursor": ""}},
    ],
)
async def test_invalid_later_page_never_returns_partial_inventory(
    page: JsonObject, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = AsyncMock(side_effect=[{"result": {"tools": [{"name": "valid"}], "nextCursor": "next"}}, page])
    monkeypatch.setattr(MCPClient, "_detect_and_request", request)
    with pytest.raises(MCPClientError, match="MCP discovery failed"):
        await MCPClient("https://mcp.example.com").list_tools()


@pytest.mark.parametrize(
    "second",
    [
        {"result": {"tools": [{"name": "same"}]}},
        {"result": {"tools": [], "nextCursor": "next"}},
    ],
)
async def test_duplicate_identity_and_cursor_cycles_fail(second: JsonObject, monkeypatch: pytest.MonkeyPatch) -> None:
    request = AsyncMock(side_effect=[{"result": {"tools": [{"name": "same"}], "nextCursor": "next"}}, second])
    monkeypatch.setattr(MCPClient, "_detect_and_request", request)
    with pytest.raises(MCPClientError, match="MCP discovery failed"):
        await MCPClient("https://mcp.example.com").list_tools()


async def test_explicit_key_replaces_embedded_key_without_retaining_it_on_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = install_wire(monkeypatch, lambda _request: httpx.Response(200, json={"result": {"tools": []}}))
    client = MCPClient("https://8.8.8.8/mcp?apiKey=old-secret&vendor=retained", api_key="replacement-secret")
    assert await client.list_tools() == []
    assert "old-secret" not in client.server_url
    assert all("old-secret" not in str(request.url) for request in requests)
    assert all(request.headers["authorization"] == "Bearer replacement-secret" for request in requests)
    assert "vendor=retained" in client.server_url


async def test_sse_cursor_pages_keep_one_session(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions: list[str] = []
    cursors: list[object] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            sessions.append(str(len(sessions) + 1))
            return httpx.Response(200, text=f"event: endpoint\ndata: /messages?session={sessions[-1]}\n\n")
        body = json.loads(request.content)
        if body["method"] != "tools/list":
            return httpx.Response(200, json={"result": {}})
        cursor = body["params"].get("cursor")
        cursors.append(cursor)
        if cursor is None:
            result = {"tools": [{"name": "first", "inputSchema": {}}], "nextCursor": "session:1:next"}
        elif request.url.params["session"] == "1" and cursor == "session:1:next":
            result = {"tools": [{"name": "last", "inputSchema": {}}]}
        else:
            return httpx.Response(200, json={"error": {"message": "cursor belongs to another session"}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    requests = install_wire(monkeypatch, respond)
    client = MCPClient("https://8.8.8.8/sse")
    client._transport = "sse"
    tools = await client.list_tools()
    assert [tool["name"] for tool in tools] == ["first", "last"]
    assert sessions == ["1"]
    assert cursors == [None, "session:1:next"]
    assert [json.loads(request.content)["method"] for request in requests if request.method == "POST"].count(
        "initialize"
    ) == 1


@pytest.mark.parametrize("cancel", [False, True])
async def test_sse_pagination_failure_and_cancellation_close_the_stream(
    monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    closed: list[bool] = []

    class SSEStream(httpx.AsyncByteStream):
        @override
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"event: endpoint\ndata: /messages?session=one\n\n"

        @override
        async def aclose(self) -> None:
            closed.append(True)

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, stream=SSEStream())
        body = json.loads(request.content)
        if body["method"] == "tools/list":
            if body["params"].get("cursor"):
                if cancel:
                    raise anyio.get_cancelled_exc_class()
                return httpx.Response(200, json={"error": {"message": "later page failed"}})
            return httpx.Response(200, json={"result": {"tools": [], "nextCursor": "next"}})
        return httpx.Response(200, json={"result": {}})

    install_wire(monkeypatch, respond)
    client = MCPClient("https://8.8.8.8/sse")
    client._transport = "sse"
    with pytest.raises(anyio.get_cancelled_exc_class() if cancel else MCPClientError):
        await client.list_tools()
    assert closed == [True]
