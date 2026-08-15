"""Composed builtin tool definitions."""


from typing import Any

from app.services.llm.finish import FINISH_TOOL_SEED
from app.services.tool_definitions.agentbay import AGENTBAY_TOOLS
from app.services.tool_definitions.aware import AWARE_TOOLS
from app.services.tool_definitions.code import CODE_TOOLS
from app.services.tool_definitions.communication import COMMUNICATION_TOOLS
from app.services.tool_definitions.deploy import DEPLOY_BUILTIN_TOOLS
from app.services.tool_definitions.email import EMAIL_TOOLS
from app.services.tool_definitions.feishu_tools import FEISHU_SEED_TOOLS
from app.services.tool_definitions.file import FILE_TOOLS
from app.services.tool_definitions.mcp import MCP_TOOLS
from app.services.tool_definitions.media import MEDIA_TOOLS
from app.services.tool_definitions.okr import OKR_BUILTIN_TOOLS
from app.services.tool_definitions.okr_inline import OKR_INLINE_TOOLS
from app.services.tool_definitions.okr_stray import OKR_STRAY_TOOLS
from app.services.tool_definitions.pages import PAGES_TOOLS
from app.services.tool_definitions.search import SEARCH_TOOLS
from app.services.tool_definitions.skills import SKILL_TOOLS
from app.services.tool_definitions.social import SOCIAL_TOOLS

# Builtin tool definitions - these map to the hardcoded AGENT_TOOLS
BUILTIN_TOOLS: list[dict[str, Any]] = [
    FINISH_TOOL_SEED,
    *FILE_TOOLS,
    *AWARE_TOOLS,
    *COMMUNICATION_TOOLS,
    *SEARCH_TOOLS,
    *SOCIAL_TOOLS,
    *CODE_TOOLS,
    *MEDIA_TOOLS,
    *MCP_TOOLS,
    *EMAIL_TOOLS,
    *OKR_INLINE_TOOLS,
    *FEISHU_SEED_TOOLS,
    *PAGES_TOOLS,
    *SKILL_TOOLS,
    *OKR_STRAY_TOOLS,
    *AGENTBAY_TOOLS,
    *OKR_BUILTIN_TOOLS,
    *DEPLOY_BUILTIN_TOOLS,
]
