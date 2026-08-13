from app.core.json_types import JsonObject
from app.services import resource_discovery


def test_discovered_mcp_tool_normalization_uses_safe_description_and_schema_defaults():
    # Given: an MCP server returned JSON values outside the persisted Tool contract.
    raw_tool: JsonObject = {
        "name": "lookup_issue",
        "description": {"unexpected": "object"},
        "inputSchema": {"type": "object", "properties": ["unexpected"]},
    }

    # When: the discovery boundary normalizes the externally supplied tool.
    normalized = resource_discovery._normalize_discovered_mcp_tool(raw_tool)

    # Then: only values accepted by the Tool model cross the boundary.
    assert normalized == {
        "name": "lookup_issue",
        "description": "",
        "parameters_schema": {"type": "object", "properties": {}},
    }
