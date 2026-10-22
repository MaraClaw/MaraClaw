from collections.abc import Mapping
from typing import Annotated, Literal, TypedDict

from app.core.json_types import JsonObject, JsonValue
from app.schemas.operator_response_contract import response_contract
from app.services.atlassian_mcp_tools import AtlassianToolPreview


class ToolAcknowledgement(TypedDict):
    ok: Literal[True]


class ToolCreatedResponse(TypedDict):
    id: str
    name: str


class MCPServerUpdatedResponse(ToolAcknowledgement):
    updated: int


class AgentToolResponse(TypedDict):
    id: str
    name: str
    display_name: str
    description: str
    type: str
    category: str
    icon: str
    enabled: bool
    is_default: bool
    mcp_server_name: str | None
    mcp_server_url: str | None
    source: str


class ToolCapabilities(TypedDict):
    can_edit_details: bool
    can_delete: bool
    can_configure: bool


class ToolCatalogResponse(AgentToolResponse):
    tenant_id: str | None
    capabilities: ToolCapabilities
    parameters_schema: JsonObject
    mcp_tool_name: str | None
    config: JsonObject
    config_schema: JsonObject
    created_at: str | None


class AgentToolWithConfigResponse(AgentToolResponse):
    agent_tool_id: str | None
    config_schema: JsonObject
    global_config: JsonObject
    agent_config: JsonObject


class AgentInstalledToolResponse(TypedDict):
    agent_tool_id: str
    agent_id: str
    tool_id: str
    tool_name: str
    tool_display_name: str
    description: str
    type: str
    category: str
    source: str
    mcp_server_name: str | None
    mcp_server_url: str | None
    mcp_tool_name: str | None
    installed_by_agent_id: str | None
    installed_by_agent_name: str | None
    enabled: bool
    configured: bool
    installed_at: str | None


class AgentToolConfigResponse(TypedDict):
    global_config: JsonObject
    agent_config: JsonObject
    merged_config: JsonObject
    config_schema: JsonObject


class ToolTestFailure(TypedDict):
    ok: Literal[False]
    error: str


class MCPTestSuccess(TypedDict):
    ok: Literal[True]
    tools: list[JsonObject]


type MCPTestResponse = MCPTestSuccess | ToolTestFailure


class EmailProviderHelp(TypedDict):
    label: str
    help_url: str
    help_text: str


class CategoryConfigResponse(TypedDict):
    id: str | None
    agent_id: str
    category: str
    is_configured: bool
    config: JsonObject
    global_config: JsonObject
    agent_config: JsonObject


class CategoryTestSuccess(TypedDict):
    ok: Literal[True]
    message: str


class AtlassianChannelResponse(TypedDict):
    id: str
    agent_id: str
    channel_type: str
    is_configured: bool
    is_connected: bool
    cloud_id: JsonValue
    extra_config: JsonObject
    created_at: str | None


class AtlassianTestSuccess(CategoryTestSuccess):
    tool_count: int
    tools: list[AtlassianToolPreview]


type AtlassianTestResponse = AtlassianTestSuccess | ToolTestFailure
type CategoryTestResponse = AtlassianTestResponse | CategoryTestSuccess
type CategoryTestPassthrough = Annotated[Mapping[str, object], response_contract(CategoryTestResponse)]
