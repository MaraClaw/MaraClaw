import json
from collections.abc import AsyncIterator
from typing import override
from unittest.mock import AsyncMock

import anyio
import httpx
import pytest

from app.core.json_types import JsonObject, JsonValue
from app.services.mcp_client import (
    MCP_MAX_TOOL_TEXT_CHARS,
    MCP_TOOL_TEXT_TRUNCATION_MARKER,
    MCPClient,
    MCPClientError,
)
from app.services.mcp_inventory import (
    MCP_INVENTORY_MAX_PAGES,
    MCP_INVENTORY_MAX_TOOLS,
    MCPInventoryError,
    complete_inventory,
)
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


def tools_page(first: int, count: int, cursor: str | None) -> JsonObject:
    tools: list[JsonValue] = [{"name": f"tool-{index}"} for index in range(first, first + count)]
    result: JsonObject = {"tools": tools}
    if cursor is not None:
        result["nextCursor"] = cursor
    return {"result": result}


def paged(sizes: list[int], *, last_cursor: str | None = None) -> list[JsonObject]:
    pages: list[JsonObject] = []
    first = 0
    for number, size in enumerate(sizes):
        cursor = f"after-{number}" if number + 1 < len(sizes) else last_cursor
        pages.append(tools_page(first, size, cursor))
        first += size
    return pages


async def test_inventory_accepts_exactly_the_page_and_item_limits() -> None:
    sizes = [MCP_INVENTORY_MAX_TOOLS - MCP_INVENTORY_MAX_PAGES + 1] + [1] * (MCP_INVENTORY_MAX_PAGES - 1)
    request = AsyncMock(side_effect=paged(sizes))

    inventory = await complete_inventory(request)

    assert len(inventory) == MCP_INVENTORY_MAX_TOOLS
    assert request.await_count == MCP_INVENTORY_MAX_PAGES


async def test_inventory_still_paging_after_the_page_limit_fails_closed() -> None:
    # Given: every page, the last allowed one included, points at another page.
    request = AsyncMock(side_effect=paged([1] * MCP_INVENTORY_MAX_PAGES, last_cursor="more"))

    # When / Then: the walk stops at the limit and raises instead of returning the pages it read.
    with pytest.raises(MCPInventoryError):
        await complete_inventory(request)
    assert request.await_count == MCP_INVENTORY_MAX_PAGES


@pytest.mark.parametrize(
    "sizes",
    [[MCP_INVENTORY_MAX_TOOLS + 1], [MCP_INVENTORY_MAX_TOOLS, 1], [MCP_INVENTORY_MAX_TOOLS - 1, 2]],
    ids=["one-page", "exact-then-one", "crossing-page"],
)
async def test_inventory_over_the_item_limit_fails_closed(sizes: list[int]) -> None:
    request = AsyncMock(side_effect=paged(sizes))

    with pytest.raises(MCPInventoryError):
        await complete_inventory(request)
    assert request.await_count == len(sizes)


async def test_discovery_over_the_page_limit_returns_no_partial_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    request = AsyncMock(side_effect=paged([1] * (MCP_INVENTORY_MAX_PAGES + 1)))
    monkeypatch.setattr(MCPClient, "_detect_and_request", request)

    with pytest.raises(MCPClientError, match="MCP discovery failed"):
        await MCPClient("https://mcp.example.com").list_tools()
    assert request.await_count == MCP_INVENTORY_MAX_PAGES


@pytest.mark.parametrize("extra", [0, 1, 5_000])
async def test_joined_tool_text_is_capped_with_a_truncation_marker(monkeypatch: pytest.MonkeyPatch, extra: int) -> None:
    # Given: two text blocks whose joined text is the cap plus ``extra`` characters.
    first = "a" * 60_000
    second = "b" * (MCP_MAX_TOOL_TEXT_CHARS - len(first) - 1 + extra)
    blocks: list[JsonValue] = [{"type": "text", "text": first}, {"type": "text", "text": second}]
    request = AsyncMock(return_value={"result": {"content": blocks}})
    monkeypatch.setattr(MCPClient, "_detect_and_request", request)

    # When
    result = await MCPClient("https://mcp.example.com").call_tool("lookup", {})

    # Then: text within the cap is untouched; longer text keeps its first cap characters plus the marker line.
    joined = f"{first}\n{second}"
    if extra == 0:
        assert result == joined
    else:
        assert result == f"{joined[:MCP_MAX_TOOL_TEXT_CHARS]}\n{MCP_TOOL_TEXT_TRUNCATION_MARKER}"
