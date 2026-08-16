"""Shared channel LLM load/call helpers.

Other IM routers historically imported these from ``app.api.feishu``.
Keep the underscore names stable and re-export them from that module.
"""

from __future__ import annotations

import asyncio
import uuid

from app.core.logging import logger
from app.core.permissions import is_agent_expired
from app.records.agent import AgentRecord
from app.records.llm import LLMModelRecord
from app.services.llm.base import ChunkCallback, ThinkingCallback, ToolCallback
from app.services.llm.turn import TurnContext
from app.services.llm.types import OpenAIMessage
from app.services.llm.utils import truncate_messages_with_pair_integrity

DEFAULT_CONTEXT_WINDOW_SIZE = 100
_LLM_TIMEOUT_SECONDS_DEFAULT = 180.0


def _get_llm_timeout(model: LLMModelRecord) -> float:
    timeout = model.request_timeout
    if timeout and timeout > 0:
        return float(timeout)
    return _LLM_TIMEOUT_SECONDS_DEFAULT


async def _load_agent_and_model(
    db: object | None, agent_id: uuid.UUID
) -> tuple[AgentRecord | None, LLMModelRecord | None, LLMModelRecord | None]:
    """Load agent and LLM model configs.

    Returns (agent, model, fallback_model). ``db`` is accepted for call-site
    compatibility and ignored (pure-psycopg path). Routing happens later in
    ``_call_llm_with_config`` so this tuple stays backward compatible.
    """
    del db
    from app.dao import agent_dao
    from app.services.llm.router import load_agent_model_bundle

    agent = await agent_dao.get(agent_id)
    if not agent:
        return None, None, None

    bundle = await load_agent_model_bundle(agent)
    model = bundle.primary if bundle.primary and bundle.primary.enabled else None
    fallback_model = bundle.fallback if bundle.fallback and bundle.fallback.enabled else None
    if model and not model.enabled:
        logger.info(f"[Channel] Primary model {model.model} is disabled, skipping")
        model = None
    if fallback_model and not fallback_model.enabled:
        logger.info(f"[Channel] Fallback model {fallback_model.model} is disabled, skipping")
        fallback_model = None
    return agent, model, fallback_model


async def _call_llm_with_config(
    agent: AgentRecord | None,
    model: LLMModelRecord | None,
    fallback_model: LLMModelRecord | None,
    agent_id: uuid.UUID,
    user_text: str,
    history: list[OpenAIMessage] | None = None,
    user_id: uuid.UUID | None = None,
    session_id: str = "",
    on_chunk: ChunkCallback | None = None,
    on_thinking: ThinkingCallback | None = None,
    on_tool_call: ToolCallback | None = None,
) -> str:
    """Call LLM with pre-loaded agent/model objects. No DB session needed."""
    from app.services.llm import call_llm_with_failover
    from app.services.llm.router import load_agent_model_bundle, select_turn_model

    if agent is None:
        return "⚠️ Agent not found."
    if is_agent_expired(agent):
        return "This Agent has expired and is off duty. Please contact your admin to extend its service."

    bundle = await load_agent_model_bundle(agent, primary=model, fallback=fallback_model)
    choice = await select_turn_model(
        bundle,
        user_text=user_text,
        history=history,
        agent_id=agent_id,
    )
    selected = choice.model
    if not selected:
        return f"⚠️ {agent.name} has no LLM model configured. Set one in the admin console."

    messages: list[OpenAIMessage] = []
    ctx_size = agent.context_window_size or DEFAULT_CONTEXT_WINDOW_SIZE
    if history:
        messages.extend(truncate_messages_with_pair_integrity(history, ctx_size))
    messages.append({"role": "user", "content": user_text})

    effective_user_id = user_id or agent_id
    timeout = _get_llm_timeout(selected)
    if choice.failover_model:
        timeout = max(timeout, _get_llm_timeout(choice.failover_model))
    turn = TurnContext(
        agent=agent,
        primary_model=bundle.primary,
        secondary_model=bundle.secondary,
        fallback_model=bundle.fallback,
        selected_model=selected,
        selected_slot=choice.slot,
        complexity=choice.complexity,
        routing_reason=choice.reason,
    )

    try:
        return await asyncio.wait_for(
            call_llm_with_failover(
                primary_model=selected,
                fallback_model=choice.failover_model,
                messages=messages,
                agent_name=agent.name,
                role_description=agent.role_description or "",
                agent_id=agent_id,
                user_id=effective_user_id,
                session_id=session_id,
                supports_vision=selected.supports_vision,
                on_chunk=on_chunk,
                on_thinking=on_thinking,
                on_tool_call=on_tool_call,
                turn=turn,
            ),
            timeout=timeout,
        )
    except TimeoutError:
        logger.error(f"[LLM] Call timed out after {timeout}s (agent_id={agent_id}, model={selected.model})")
        return f"⚠️ Model response timed out (>{int(timeout)}s). Please retry or shorten your request."
    except Exception as error:
        error_msg = str(error) or repr(error)
        logger.error(f"[LLM] Model call failed: {error_msg}")
        return f"⚠️ Model call failed: {error_msg[:150]}"


async def _call_agent_llm(
    db: object | None,
    agent_id: uuid.UUID,
    user_text: str,
    history: list[OpenAIMessage] | None = None,
    user_id: uuid.UUID | None = None,
    session_id: str = "",
    on_chunk: ChunkCallback | None = None,
    on_thinking: ThinkingCallback | None = None,
    on_tool_call: ToolCallback | None = None,
) -> str:
    agent, model, fallback_model = await _load_agent_and_model(db, agent_id)
    if not agent:
        return "⚠️ Digital employee not found"
    return await _call_llm_with_config(
        agent,
        model,
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
