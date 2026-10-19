"""Resolve the existing AgentBay tool configuration for a canonical session."""

import uuid
from typing import TYPE_CHECKING

from app.core.json_types import JsonObject, json_as_str, json_object_from
from app.core.logging import logger

if TYPE_CHECKING:
    from app.services.agentbay_client import AgentBayClient


async def configured_client(agent_id: uuid.UUID) -> tuple[AgentBayClient, JsonObject]:
    from app.config import get_settings
    from app.core.security import decrypt_data
    from app.services import agentbay_client
    from app.services.agent_tools import _get_tool_config
    from app.services.agentbay_config import is_plausible_agentbay_api_key

    config = json_object_from(await _get_tool_config(agent_id, "agentbay_browser_navigate"))
    api_key = json_as_str(config.get("api_key"))
    if api_key not in (None, ""):
        try:
            api_key = decrypt_data(api_key, get_settings().SECRET_KEY)
        except ValueError as error:
            logger.debug("[AgentBay] Using plaintext configured key: {}", type(error).__name__)
        if not is_plausible_agentbay_api_key(api_key):
            api_key = None
    if api_key in (None, ""):
        api_key = await agentbay_client.get_agentbay_api_key_for_agent(agent_id)
    if api_key in (None, ""):
        raise RuntimeError("AgentBay not configured for this agent. Please configure in Tools > AgentBay.")
    return agentbay_client.AgentBayClient(api_key), config
