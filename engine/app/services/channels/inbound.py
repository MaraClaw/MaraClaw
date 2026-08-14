"""Shared inbound chat pipeline helpers used by webhook / stream connectors."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.row_memo import clear_entity_memo
from app.dao.agent_dao import agent_dao
from app.dao.chat_dao import chat_message_dao
from app.records.agent import AgentRecord
from app.records.chat import ChatSessionRecord
from app.records.user import UserRecord
from app.services.channel_session import find_or_create_channel_session
from app.services.channel_user_service import channel_user_service
from app.services.chat_persist import persist_chat_message
from app.services.llm.utils import convert_chat_messages_to_llm_format

DEFAULT_CONTEXT_WINDOW_SIZE = 100

ChunkCallback = Callable[[str], Awaitable[None] | None]
ToolCallCallback = Callable[[Any], Awaitable[None] | None]


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
    limit = context_window_size or DEFAULT_CONTEXT_WINDOW_SIZE
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
    agent_model: Any | None = None,
    llm_model: Any | None = None,
    fallback_model: Any | None = None,
    on_chunk: ChunkCallback | None = None,
    on_thinking: ChunkCallback | None = None,
    on_tool_call: ToolCallCallback | None = None,
) -> str:
    """Run the shared channel LLM path used by Feishu / Slack / Teams / Google Chat.

    ``history`` must be prior turns only; ``user_text`` is the new user turn
    (``_call_llm_with_config`` appends it).
    """
    # Lazy import: implementation still lives under api.feishu for historical reasons.
    from app.api.feishu import _call_llm_with_config, _load_agent_and_model

    if agent_model is None and llm_model is None:
        agent_model, llm_model, fallback_model = await _load_agent_and_model(None, agent_id)
    return await _call_llm_with_config(
        agent_model,
        llm_model,
        fallback_model,
        agent_id,
        user_text,
        history=history,
        user_id=user_id,
        session_id=session_id,
        on_chunk=on_chunk,
        on_thinking=on_thinking,
        on_tool_call=on_tool_call,
    )


async def run_text_turn(
    *,
    agent: AgentRecord,
    agent_id: uuid.UUID,
    platform_user: UserRecord,
    session: ChatSessionRecord,
    user_text: str,
    llm_user_text: str | None = None,
    on_chunk: ChunkCallback | None = None,
    on_thinking: ChunkCallback | None = None,
    on_tool_call: ToolCallCallback | None = None,
    agent_model: Any | None = None,
    llm_model: Any | None = None,
    fallback_model: Any | None = None,
    persist_user: bool = True,
    touch_last_active_on_reply: bool = True,
) -> str:
    """Load history → persist user (optional) → LLM → persist assistant.

    History is loaded *before* persisting the current user message so
    ``generate_channel_reply`` receives prior turns only.
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
    await persist_assistant_message(
        agent_id=agent_id,
        user_id=platform_user.id,
        session=session,
        content=reply_text,
        agent=agent,
        touch_last_active=touch_last_active_on_reply,
    )
    return reply_text
