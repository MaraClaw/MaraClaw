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
    compatibility and ignored (pure-psycopg path).
    """
    del db
    from app.dao import agent_dao, llm_model_dao

    agent = await agent_dao.get(agent_id)
    if not agent:
        return None, None, None

    model_ids = [mid for mid in (agent.primary_model_id, agent.fallback_model_id) if mid]
    loaded = {row.id: row for row in await llm_model_dao.get_many(model_ids)}
    model = loaded.get(agent.primary_model_id) if agent.primary_model_id else None
    if model and not model.enabled:
        logger.info(f"[Channel] Primary model {model.model} is disabled, skipping")
        model = None

    fallback_model = loaded.get(agent.fallback_model_id) if agent.fallback_model_id else None
    if fallback_model and not fallback_model.enabled:
        logger.info(f"[Channel] Fallback model {fallback_model.model} is disabled, skipping")
        fallback_model = None

    if not model and fallback_model:
        model = fallback_model
        fallback_model = None
        logger.warning(f"[Channel] Primary model unavailable, using fallback: {model.model}")

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
    from app.services.llm import call_llm

    if agent is None:
        return "⚠️ Agent not found."
    if is_agent_expired(agent):
        return "This Agent has expired and is off duty. Please contact your admin to extend its service."

    if not model:
        return f"⚠️ {agent.name} has no LLM model configured. Set one in the admin console."

    messages: list[OpenAIMessage] = []
    ctx_size = agent.context_window_size or DEFAULT_CONTEXT_WINDOW_SIZE
    if history:
        messages.extend(truncate_messages_with_pair_integrity(history, ctx_size))
    messages.append({"role": "user", "content": user_text})

    effective_user_id = user_id or agent_id
    timeout = _get_llm_timeout(model)
    turn = TurnContext(agent=agent, primary_model=model, fallback_model=fallback_model)

    try:
        return await asyncio.wait_for(
            call_llm(
                model,
                messages,
                agent.name,
                agent.role_description or "",
                agent_id=agent_id,
                user_id=effective_user_id,
                session_id=session_id,
                supports_vision=model.supports_vision,
                on_chunk=on_chunk,
                on_thinking=on_thinking,
                on_tool_call=on_tool_call,
                turn=turn,
            ),
            timeout=timeout,
        )
    except TimeoutError:
        logger.error(f"[LLM] Call timed out after {timeout}s (agent_id={agent_id}, model={model.model})")
        if fallback_model:
            fallback_timeout = _get_llm_timeout(fallback_model)
            logger.info(
                f"[LLM] Retrying timed-out request with fallback model: {fallback_model.model} "
                + f"(timeout={fallback_timeout}s)"
            )
            try:
                return await asyncio.wait_for(
                    call_llm(
                        fallback_model,
                        messages,
                        agent.name,
                        agent.role_description or "",
                        agent_id=agent_id,
                        user_id=effective_user_id,
                        session_id=session_id,
                        supports_vision=fallback_model.supports_vision,
                        on_chunk=on_chunk,
                        on_thinking=on_thinking,
                        on_tool_call=on_tool_call,
                        turn=turn,
                    ),
                    timeout=fallback_timeout,
                )
            except TimeoutError:
                logger.error(
                    f"[LLM] Fallback call also timed out after {fallback_timeout}s "
                    + f"(agent_id={agent_id}, model={fallback_model.model})"
                )
                return f"⚠️ Model response timed out (>{int(fallback_timeout)}s). Please retry or shorten your request."
            except Exception as fallback_error:
                import traceback

                traceback.print_exc()
                return f"⚠️ Model error: Primary Timeout | Fallback: {str(fallback_error)[:80]}"
        return f"⚠️ Model response timed out (>{int(timeout)}s). Please retry or shorten your request."
    except Exception as error:
        import traceback

        traceback.print_exc()
        error_msg = str(error) or repr(error)
        logger.error(f"[LLM] Primary model error: {error_msg}")
        if fallback_model:
            logger.info(f"[LLM] Retrying with fallback model: {fallback_model.model}")
            fallback_timeout = _get_llm_timeout(fallback_model)
            try:
                return await asyncio.wait_for(
                    call_llm(
                        fallback_model,
                        messages,
                        agent.name,
                        agent.role_description or "",
                        agent_id=agent_id,
                        user_id=effective_user_id,
                        session_id=session_id,
                        supports_vision=fallback_model.supports_vision,
                        on_chunk=on_chunk,
                        on_thinking=on_thinking,
                        on_tool_call=on_tool_call,
                        turn=turn,
                    ),
                    timeout=fallback_timeout,
                )
            except TimeoutError:
                logger.error(
                    f"[LLM] Fallback call timed out after {fallback_timeout}s "
                    + f"(agent_id={agent_id}, model={fallback_model.model})"
                )
                return f"⚠️ Model error: Primary: {str(error)[:80]} | Fallback Timeout"
            except Exception as fallback_error:
                traceback.print_exc()
                return f"⚠️ Model error: Primary: {str(error)[:80]} | Fallback: {str(fallback_error)[:80]}"
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
