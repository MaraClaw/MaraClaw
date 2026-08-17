"""Shared inbound chat pipeline helpers used by webhook / stream connectors."""

from __future__ import annotations

import uuid
from typing import Any

from app.core.row_memo import clear_entity_memo
from app.dao.agent_dao import agent_dao
from app.dao.chat_dao import chat_message_dao
from app.records.agent import AgentRecord
from app.records.chat import ChatSessionRecord
from app.records.llm import LLMModelRecord
from app.records.user import UserRecord
from app.services.channel_session import find_or_create_channel_session
from app.services.channel_user_service import channel_user_service
from app.services.chat_persist import persist_chat_message
from app.services.llm.base import ChunkCallback, ThinkingCallback, ToolCallback
from app.services.llm.utils import convert_chat_messages_to_llm_format

DEFAULT_CONTEXT_WINDOW_SIZE = 100
ROUTING_HISTORY_LIMIT = 8
CHANNEL_REPLY_QUEUED = "Message forwarded to the agent. Waiting for response..."


def routing_history_limit(context_window_size: int | None = None) -> int:
    """How many prior turns to load for OpenClaw slot routing.

    Guests receive conversation history from ``/api/gateway/poll``. Engine-side
    history is only for the heuristic preflight, so a long window just delays
    the wake.
    """
    if isinstance(context_window_size, int) and context_window_size > 0:
        wanted = context_window_size
    else:
        wanted = DEFAULT_CONTEXT_WINDOW_SIZE
    return min(wanted, ROUTING_HISTORY_LIMIT)


def is_queued_channel_reply(text: str) -> bool:
    """True when inbound enqueue returned the waiting stub, not a real answer."""
    return text.strip() == CHANNEL_REPLY_QUEUED


async def load_agent(agent_id: uuid.UUID) -> AgentRecord | None:
    clear_entity_memo()
    return await agent_dao.get(agent_id)


async def resolve_sender_user(
    *,
    agent: AgentRecord,
    channel_type: str,
    external_user_id: str,
    extra_info: dict[str, Any] | None = None,
) -> UserRecord:
    return await channel_user_service.resolve_channel_user(
        db=None,
        agent=agent,
        channel_type=channel_type,
        external_user_id=external_user_id,
        extra_info=extra_info or {},
    )


async def open_channel_session(
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    external_conv_id: str,
    source_channel: str,
    first_message_title: str,
    is_group: bool = False,
    group_name: str | None = None,
) -> ChatSessionRecord:
    return await find_or_create_channel_session(
        db=None,
        agent_id=agent_id,
        user_id=user_id,
        external_conv_id=external_conv_id,
        source_channel=source_channel,
        first_message_title=first_message_title,
        is_group=is_group,
        group_name=group_name,
    )


async def load_history_for_session(
    *,
    agent_id: uuid.UUID,
    session: ChatSessionRecord,
    context_window_size: int | None,
) -> list[Any]:
    """Load prior turns for a session (does not include the current user message)."""
    limit = routing_history_limit(context_window_size)
    history_msgs = await chat_message_dao.list_recent(
        agent_id=agent_id,
        conversation_id=str(session.id),
        limit=limit,
    )
    return convert_chat_messages_to_llm_format(history_msgs)


async def persist_user_message(
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session: ChatSessionRecord,
    content: str,
    thinking: str | None = None,
    participant_id: uuid.UUID | None = None,
    agent: AgentRecord | None = None,
    touch_last_active: bool = False,
) -> None:
    await persist_chat_message(
        agent_id=agent_id,
        user_id=user_id,
        conversation_id=str(session.id),
        role="user",
        content=content,
        thinking=thinking,
        participant_id=participant_id,
        touch_last_active=touch_last_active,
        agent=agent,
    )


async def persist_assistant_message(
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session: ChatSessionRecord,
    content: str,
    thinking: str | None = None,
    participant_id: uuid.UUID | None = None,
    agent: AgentRecord | None = None,
    touch_last_active: bool = True,
) -> None:
    await persist_chat_message(
        agent_id=agent_id,
        user_id=user_id,
        conversation_id=str(session.id),
        role="assistant",
        content=content,
        thinking=thinking,
        participant_id=participant_id,
        touch_last_active=touch_last_active,
        agent=agent,
    )


async def generate_channel_reply(
    *,
    agent_id: uuid.UUID,
    user_text: str,
    history: list[Any],
    user_id: uuid.UUID,
    session_id: str,
    agent_model: AgentRecord | None = None,
    llm_model: LLMModelRecord | None = None,
    fallback_model: LLMModelRecord | None = None,
    on_chunk: ChunkCallback | None = None,
    on_thinking: ThinkingCallback | None = None,
    on_tool_call: ToolCallback | None = None,
) -> str:
    """Queue the inbound turn for the OpenClaw guest. Reply arrives via gateway report."""
    from typing import cast

    from app.dao.agent_dao import agent_dao
    from app.services.llm.types import OpenAIMessage
    from app.services.openclaw_routing import enqueue_openclaw_message

    del llm_model, fallback_model, on_chunk, on_thinking, on_tool_call
    target = agent_model
    if target is None:
        target = await agent_dao.get(agent_id)
    if target is None:
        return "⚠️ Agent not found."
    prior = cast(list[OpenAIMessage], history[-ROUTING_HISTORY_LIMIT:]) if history else None
    _ = await enqueue_openclaw_message(
        agent=target,
        content=user_text,
        sender_user_id=user_id,
        conversation_id=session_id,
        history=prior,
        await_wake=False,
    )
    return CHANNEL_REPLY_QUEUED


async def run_text_turn(
    *,
    agent: AgentRecord,
    agent_id: uuid.UUID,
    platform_user: UserRecord,
    session: ChatSessionRecord,
    user_text: str,
    llm_user_text: str | None = None,
    on_chunk: ChunkCallback | None = None,
    on_thinking: ThinkingCallback | None = None,
    on_tool_call: ToolCallback | None = None,
    agent_model: AgentRecord | None = None,
    llm_model: LLMModelRecord | None = None,
    fallback_model: LLMModelRecord | None = None,
    persist_user: bool = True,
    touch_last_active_on_reply: bool = True,
) -> str:
    """Load history → persist user (optional) → enqueue for the OpenClaw guest.

    History is loaded *before* persisting the current user message so
    ``generate_channel_reply`` receives prior turns only. The assistant
    reply is persisted later when the guest reports.
    """
    history = await load_history_for_session(
        agent_id=agent_id,
        session=session,
        context_window_size=agent.context_window_size,
    )
    if persist_user:
        await persist_user_message(
            agent_id=agent_id,
            user_id=platform_user.id,
            session=session,
            content=user_text,
            agent=agent,
        )
    reply_text = await generate_channel_reply(
        agent_id=agent_id,
        user_text=llm_user_text if llm_user_text is not None else user_text,
        history=history,
        user_id=platform_user.id,
        session_id=str(session.id),
        agent_model=agent_model,
        llm_model=llm_model,
        fallback_model=fallback_model,
        on_chunk=on_chunk,
        on_thinking=on_thinking,
        on_tool_call=on_tool_call,
    )
    del touch_last_active_on_reply
    return reply_text
