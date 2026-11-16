from pathlib import Path

import anyio

from app.dao.channel_event_media_dao import channel_event_media_dao
from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.services.agent_manager import agent_manager
from app.services.channels.channel_media import materialize_for_guest
from app.services.openclaw_transcript import open_transcript
from app.services.openclaw_turns import runtime_session_key
from app.services.shared_chat import shared_session_id


async def channel_input(agent: AgentRecord, message: GatewayMessageRecord) -> tuple[str, str]:
    if message.conversation_id != str(shared_session_id(agent.id)):
        return message.content, ""
    files = await channel_event_media_dao.for_message(message.id, agent.id)
    refs = tuple([await materialize_for_guest(agent, file) for file in files])
    paths = "\n".join(f"/home/node/custom-state/{ref.path}" for ref in refs)
    content = (
        f"{message.content}\n\nAttached files available to ordinary file tools:\n{paths}" if refs else message.content
    )
    instruction = (
        "For this Zalo turn, return generated files using standalone lines of the exact form "
        "MEDIA:/home/node/custom-state/workspace/<path>. Use real files within this workspace. "
        "The attached files are available to ordinary file tools; no automatic image analysis is implied."
        if await channel_event_media_dao.is_zalo_turn(message.id, agent.id)
        else ""
    )
    return content, instruction


def _terminal_text(root: Path, message: GatewayMessageRecord) -> str:
    try:
        with open_transcript(root) as source:
            terminal = source.terminal(runtime_session_key(message), str(message.id))
    except FileNotFoundError:
        return ""
    return terminal.text if terminal is not None else ""


async def native_completion_result(agent: AgentRecord, message: GatewayMessageRecord, text: str) -> str:
    if "MEDIA:" in text:
        return text
    terminal = await anyio.to_thread.run_sync(_terminal_text, agent_manager._agent_dir(agent.id), message)
    if "MEDIA:" in terminal:
        return terminal
    return text or "The agent completed the request without a text response."
