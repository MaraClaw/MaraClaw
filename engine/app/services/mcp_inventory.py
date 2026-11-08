"""Validate complete MCP tools/list inventories without accepting partial pages."""

from collections.abc import Awaitable, Callable

from pydantic import ValidationError

from app.core.json_types import JsonObject, is_json_object
from app.schemas.tool_management import MCPDiscoveredTool


class MCPInventoryError(ValueError):
    """A server returned an invalid or non-progressing inventory."""


def inventory_page(data: JsonObject) -> tuple[list[MCPDiscoveredTool], str | None]:
    if "error" in data:
        raise MCPInventoryError("MCP discovery failed")
    result = data.get("result")
    if not is_json_object(result):
        raise MCPInventoryError("Invalid MCP inventory page")
    tools = result.get("tools")
    if not isinstance(tools, list):
        raise MCPInventoryError("Invalid MCP inventory page")
    normalized: list[MCPDiscoveredTool] = []
    for tool in tools:
        try:
            parsed = MCPDiscoveredTool.model_validate(tool)
        except ValidationError:
            raise MCPInventoryError("Invalid MCP tool definition") from None
        normalized.append(parsed)
    cursor = result.get("nextCursor")
    if cursor is not None and (not isinstance(cursor, str) or not cursor):
        raise MCPInventoryError("Invalid MCP inventory cursor")
    return normalized, cursor


async def complete_inventory(request: Callable[[str, JsonObject], Awaitable[JsonObject]]) -> list[JsonObject]:
    inventory: list[JsonObject] = []
    names: set[str] = set()
    cursors: set[str] = set()
    params: JsonObject = {}
    while True:
        page, cursor = inventory_page(await request("tools/list", params))
        for tool in page:
            if tool.name in names:
                raise MCPInventoryError("Duplicate MCP tool identity")
            names.add(tool.name)
            inventory.append({"name": tool.name, "description": tool.description, "inputSchema": tool.input_schema})
        if cursor is None:
            return inventory
        if cursor in cursors:
            raise MCPInventoryError("Non-progressing MCP inventory cursor")
        cursors.add(cursor)
        params = {"cursor": cursor}
