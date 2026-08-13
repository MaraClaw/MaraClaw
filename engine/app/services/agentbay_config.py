"""AgentBay configuration resolution."""

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.config import get_settings
from app.core.security import decrypt_data
from app.dao.channel_config_dao import channel_config_dao
from app.dao.tool_dao import tool_dao
from app.records.tool import ToolRecord


class AgentBayConfigSource(StrEnum):
    """Locations from which an AgentBay key can be resolved."""

    PER_AGENT_CHANNEL = "per_agent_channel"
    BROWSER_NAVIGATE_TOOL = "browser_navigate_tool"
    AGENTBAY_TOOL_SCAN = "agentbay_tool_scan"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class AgentBayConfigResolution:
    """The source and optional key selected for an AgentBay request."""

    source: AgentBayConfigSource
    api_key: str | None


def is_plausible_agentbay_api_key(value: str | None) -> bool:
    """Return whether a value has the AgentBay API key format."""

    return bool(value is not None and value.strip().startswith("akm-"))


def _resolve_stored_api_key(stored_value: str) -> str | None:
    try:
        candidate = decrypt_data(stored_value, get_settings().SECRET_KEY)
    except ValueError:
        candidate = stored_value

    if is_plausible_agentbay_api_key(candidate):
        return candidate
    return None


def _tool_api_key(tool: ToolRecord) -> str | None:
    value = (tool.config or {}).get("api_key")
    if isinstance(value, str):
        return value
    return None


async def _resolve_with_daos(agent_id: uuid.UUID | None) -> AgentBayConfigResolution:
    if agent_id is not None:
        channel_config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="agentbay")
        if channel_config is not None and channel_config.is_configured and channel_config.app_secret:
            api_key = _resolve_stored_api_key(channel_config.app_secret)
            if api_key is not None:
                return AgentBayConfigResolution(AgentBayConfigSource.PER_AGENT_CHANNEL, api_key)

    browser_tool = await tool_dao.get_enabled_by_name("agentbay_browser_navigate")
    agentbay_tools = await tool_dao.list_enabled_by_category("agentbay")

    candidate_tools: list[tuple[ToolRecord, AgentBayConfigSource]] = []
    if browser_tool is not None:
        candidate_tools.append((browser_tool, AgentBayConfigSource.BROWSER_NAVIGATE_TOOL))
    candidate_tools.extend(
        (tool, AgentBayConfigSource.AGENTBAY_TOOL_SCAN)
        for tool in agentbay_tools
        if browser_tool is None or tool.id != browser_tool.id
    )

    for tool, source in candidate_tools:
        stored_api_key = _tool_api_key(tool)
        if stored_api_key is None:
            continue
        api_key = _resolve_stored_api_key(stored_api_key)
        if api_key is not None:
            return AgentBayConfigResolution(source, api_key)

    return AgentBayConfigResolution(AgentBayConfigSource.UNRESOLVED, None)


async def resolve_agentbay_config(
    agent_id: uuid.UUID | None,
    db: Any = None,
) -> AgentBayConfigResolution:
    """Resolve AgentBay configuration from per-agent and global persisted settings.

    ``db`` is accepted for dual-stack call-site compatibility and ignored.
    """
    return await _resolve_with_daos(agent_id)
