from app.services.agent_tools_definitions.agentbay import AGENTBAY_AGENT_TOOLS
from app.services.agent_tools_definitions.code_media import CODE_MEDIA_AGENT_TOOLS
from app.services.agent_tools_definitions.core import CORE_AGENT_TOOLS
from app.services.agent_tools_definitions.core_2 import CORE_AGENT_TOOLS_2
from app.services.agent_tools_definitions.email import EMAIL_AGENT_TOOLS
from app.services.agent_tools_definitions.feishu import FEISHU_AGENT_TOOLS
from app.services.agent_tools_definitions.mcp import MCP_AGENT_TOOLS
from app.services.agent_tools_definitions.mcp_2 import MCP_AGENT_TOOLS_2
from app.services.agent_tools_definitions.messaging import MESSAGING_AGENT_TOOLS
from app.services.agent_tools_definitions.pages import PAGES_AGENT_TOOLS
from app.services.agent_tools_definitions.skills import SKILL_AGENT_TOOLS
from app.services.agent_tools_definitions.triggers import TRIGGER_AGENT_TOOLS
from app.services.llm.finish import FINISH_TOOL_DEFINITION, FINISH_TOOL_NAME

# allow: SIZE_OK - static OpenAI tool catalog extracted verbatim; composed data table.
# ─── Tool Definitions (OpenAI function-calling format) ──────────

AGENT_TOOLS: list[object] = [
    FINISH_TOOL_DEFINITION,
    *CORE_AGENT_TOOLS,
    *TRIGGER_AGENT_TOOLS,
    *MESSAGING_AGENT_TOOLS,
    *CORE_AGENT_TOOLS_2,
    *CODE_MEDIA_AGENT_TOOLS,
    *MCP_AGENT_TOOLS,
    *FEISHU_AGENT_TOOLS,
    *MCP_AGENT_TOOLS_2,
    *EMAIL_AGENT_TOOLS,
    *PAGES_AGENT_TOOLS,
    *SKILL_AGENT_TOOLS,
    *AGENTBAY_AGENT_TOOLS,
]

# Core tools that should always be available to agents regardless of
# DB configuration.
# Note: send_channel_message is intentionally NOT here - it lives in
# _CHANNEL_MESSAGE_TOOL_NAMES and is only added when a channel is configured,
# to avoid sending duplicate tool definitions to the LLM.
_ALWAYS_INCLUDE_CORE: set[str] = {
    "complete_focus_item",
    FINISH_TOOL_NAME,
    "list_focus_items",
    "send_channel_file",
    "send_file_to_agent",
    "upsert_focus_item",
    "write_file",
}
# Channel message tool - available when any channel (Feishu/DingTalk/WeCom) is configured
_CHANNEL_MESSAGE_TOOL_NAMES: set[str] = {
    "send_channel_message",
}
# Feishu tools are ONLY included when the agent has a configured Feishu channel,
# to avoid exposing unnecessary tools to non-Feishu agents (reduces hallucination risk).
_FEISHU_TOOL_NAMES: set[str] = {
    "send_feishu_message",
    "feishu_user_search",
    # Seeded/configured Feishu tool name retained for DB gating; no static definition exists.
    "bitable_create_app",
    "bitable_list_tables",
    "bitable_list_fields",
    "bitable_query_records",
    "bitable_create_record",
    "bitable_update_record",
    "bitable_delete_record",
    "feishu_doc_search",
    "feishu_wiki_list",
    "feishu_doc_read",
    "feishu_doc_create",
    "feishu_doc_append",
    "feishu_drive_share",
    "feishu_drive_delete",
    "feishu_calendar_list",
    "feishu_calendar_create",
    "feishu_calendar_update",
    "feishu_calendar_delete",
    "feishu_approval_create",
    "feishu_approval_query",
    "feishu_approval_get",
}


def _catalog_tool_name(tool: object) -> str:
    if not isinstance(tool, dict):
        return ""
    function = tool.get("function")
    if not isinstance(function, dict):
        return ""
    name = function.get("name")
    return name if isinstance(name, str) else ""


_always_core_tools: list[object] = [t for t in AGENT_TOOLS if _catalog_tool_name(t) in _ALWAYS_INCLUDE_CORE]
_feishu_tools: list[object] = [t for t in AGENT_TOOLS if _catalog_tool_name(t) in _FEISHU_TOOL_NAMES]
_channel_tools: list[object] = [t for t in AGENT_TOOLS if _catalog_tool_name(t) in _CHANNEL_MESSAGE_TOOL_NAMES]
