from __future__ import annotations

import uuid

from .a2a_send import _send_file_to_agent, _send_message_to_agent
from .registry import ToolArguments, ToolOutputCallback, register


@register("send_message_to_agent")
async def send_message_to_agent(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del on_output
    return await _send_message_to_agent(
        agent_id,
        arguments,
        user_id=user_id,
        origin_session_id=session_id,
    )


@register("send_file_to_agent")
async def send_file_to_agent(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, session_id, on_output
    return await _send_file_to_agent(agent_id, arguments)
