import pytest

from app.core.json_types import JsonObject, JsonValue
from app.services import atlassian_mcp_tools as atlassian


def test_preview_tools_when_provider_tool_is_valid():
    # Given: an Atlassian provider response with valid tool metadata.
    provider_tools: list[JsonObject] = [{"name": "jira_search", "description": "Search Jira issues"}]

    # When: the response is prepared for the channel check result.
    previews = atlassian._preview_atlassian_tools(provider_tools)

    # Then: its user-visible shape matches the existing successful response.
    assert previews == [{"name": "jira_search", "description": "Search Jira issues"}]


def test_preview_tools_when_description_is_missing():
    # Given: an Atlassian provider response without an optional description.
    provider_tools: list[JsonObject] = [{"name": "jira_search"}]

    # When: the response is prepared for the channel check result.
    previews = atlassian._preview_atlassian_tools(provider_tools)

    # Then: the existing empty-description fallback is retained.
    assert previews == [{"name": "jira_search", "description": ""}]


def test_preview_tools_when_description_is_not_a_string():
    # Given: an Atlassian provider response with malformed description metadata.
    provider_tools: list[JsonObject] = [{"name": "jira_search", "description": 7}]

    # When: the response is prepared for the channel check result.
    with pytest.raises(TypeError) as error:
        atlassian._preview_atlassian_tools(provider_tools)

    # Then: the malformed description fails with the established error contract.
    assert str(error.value) == "Atlassian tool description must be a string"


def test_preview_tools_when_provider_returns_more_than_ten_long_descriptions():
    # Given: an Atlassian provider response beyond the public preview limits.
    provider_tools: list[JsonObject] = [{"name": f"tool_{index}", "description": "d" * 101} for index in range(11)]

    # When: the response is prepared for the channel check result.
    previews = atlassian._preview_atlassian_tools(provider_tools)

    # Then: the existing ten-tool and one-hundred-character limits are retained.
    assert len(previews) == 10
    assert previews[0]["description"] == "d" * 100


def test_normalize_tool_when_provider_metadata_is_valid():
    # Given: a valid Atlassian provider tool definition.
    provider_tool: JsonObject = {
        "name": "jira_search",
        "description": "Search Jira issues",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }

    # When: the tool is normalized before ORM assignment.
    tool = atlassian._normalize_atlassian_tool(provider_tool)

    # Then: the previously persisted values are retained exactly.
    assert tool == {
        "name": "jira_search",
        "description": "Search Jira issues",
        "parameters_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }


@pytest.mark.parametrize("invalid_name", ["", 42])
def test_normalize_tool_when_name_is_empty_or_malformed(invalid_name: JsonValue):
    # Given: a provider response with an empty or non-string tool name.
    provider_tool: JsonObject = {
        "name": invalid_name,
        "description": "Search Jira issues",
        "inputSchema": {"type": "object", "properties": {}},
    }

    # When: the tool is normalized before ORM assignment.
    tool = atlassian._normalize_atlassian_tool(provider_tool)

    # Then: the malformed name is rejected rather than coerced to a string.
    assert tool is None


def test_normalize_tool_when_description_is_malformed():
    # Given: a provider response with non-string description metadata.
    provider_tool: JsonObject = {
        "name": "jira_search",
        "description": 7,
        "inputSchema": {"type": "object", "properties": {}},
    }

    # When: the tool is normalized before ORM assignment.
    tool = atlassian._normalize_atlassian_tool(provider_tool)

    # Then: the malformed description is rejected rather than coerced to a string.
    assert tool is None


def test_normalize_tool_when_input_schema_is_missing():
    # Given: a provider response without input schema metadata.
    provider_tool: JsonObject = {"name": "jira_search", "description": "Search Jira issues"}

    # When: the tool is normalized before ORM assignment.
    tool = atlassian._normalize_atlassian_tool(provider_tool)

    # Then: the established default function schema is used.
    assert tool == {
        "name": "jira_search",
        "description": "Search Jira issues",
        "parameters_schema": {"type": "object", "properties": {}},
    }


@pytest.mark.parametrize(
    "invalid_schema",
    [
        [],
        {"type": 7, "properties": {}},
        {"type": "object", "properties": []},
        {"type": "object", "properties": {"query": []}},
        {"type": "object", "properties": {}, "required": "query"},
        {"type": "object", "properties": {}, "required": ["query", 7]},
    ],
)
def test_normalize_tool_when_input_schema_is_invalid(invalid_schema: JsonValue):
    # Given: a provider response with malformed input schema metadata.
    provider_tool: JsonObject = {
        "name": "jira_search",
        "description": "Search Jira issues",
        "inputSchema": invalid_schema,
    }

    # When: the tool is normalized before ORM assignment.
    tool = atlassian._normalize_atlassian_tool(provider_tool)

    # Then: the established default function schema is used.
    assert tool == {
        "name": "jira_search",
        "description": "Search Jira issues",
        "parameters_schema": {"type": "object", "properties": {}},
    }
