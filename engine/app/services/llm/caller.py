"""Unified LLM calling service with failover support for all execution paths.

This module provides a shared entry point for all LLM calls across:
- WebSocket chat
- IM channels (Feishu, Slack, Teams, Discord, WeCom, DingTalk)
- Background services (task executor, scheduler, heartbeat, etc.)

All paths now support:
1. Config-level fallback: if primary missing, use fallback directly
2. Runtime failover: if primary fails with retryable error, try fallback once
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from app.config import get_settings
from app.core.json_types import str_findall
from app.core.logging import logger
from app.dao.base import as_uuid
from app.records.agent import AgentRecord
from app.records.llm import LLMModelRecord
from app.services.agent_tool_exec.registry import ToolOutputCallback
from app.services.token_tracker import (
    TokenUsage,
    estimate_token_usage_from_chars,
    extract_token_usage,
    record_token_usage,
)

from .base import ChunkCallback, ThinkingCallback, ToolCallback, ToolCallbackData, ToolDefinition
from .client import LLMError
from .failover import FailoverErrorType, classify_error
from .finish import FINISH_PROTOCOL_REMINDER, FINISH_TOOL_DEFINITION, find_finish_call, parse_tool_arguments
from .turn import TurnContext
from .types import LLMContentPart, LLMResponse, LLMToolCall, OpenAIMessage, ToolPayload
from .utils import LLMMessage, create_llm_client, get_max_tokens, get_model_api_key

# NOTE: agent_tools imports are deferred to function bodies to avoid circular
# import: agent_tools → llm.finish → llm/__init__ → caller → agent_tools


async def get_agent_tools_for_llm(agent_id: uuid.UUID) -> list[ToolDefinition]:
    from app.services.agent_tools import get_agent_tools_for_llm as _impl

    return await _impl(agent_id)


async def execute_tool(
    tool_name: str,
    arguments: ToolPayload,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str = "",
    on_output: ToolOutputCallback | None = None,
) -> str:
    from app.services import agent_tools

    return await agent_tools.execute_tool(
        tool_name,
        arguments,
        agent_id,
        user_id,
        session_id=session_id,
        on_output=on_output,
    )


TOOLS_REQUIRING_ARGS = frozenset(
    {
        "write_file",
        "read_file",
        "move_file",
        "delete_file",
        "read_document",
        "send_message_to_agent",
        "send_feishu_message",
        "send_email",
    }
)


def _sanitize_tool_calls_for_context(tool_calls: list[LLMToolCall]) -> tuple[list[LLMToolCall] | None, str | None]:
    """Return OpenAI-compatible tool calls, or a retry instruction if args are invalid."""
    sanitized: list[LLMToolCall] = []
    for tc in tool_calls:
        fn = tc.get("function") or {}
        tool_name = fn.get("name") or ""
        raw_args = fn.get("arguments", "{}")

        if raw_args is None or raw_args == "":
            args_str = "{}"
        elif isinstance(raw_args, str):
            try:
                json.loads(raw_args)
            except json.JSONDecodeError as exc:
                logger.warning(
                    "[LLM] Invalid tool arguments JSON for {}: {} at pos {}",
                    tool_name or "<unknown>",
                    exc.msg,
                    exc.pos,
                )
                return None, (
                    "Your previous tool call arguments were not valid JSON. "
                    + f"The affected tool was `{tool_name or 'unknown'}`. "
                    + "Retry the tool call now with `function.arguments` as one valid JSON object string. "
                    + "Escape all quotes and newlines inside long HTML, CSS, JavaScript, or markdown content. "
                    + "Do not explain; only retry with a valid tool call."
                )
            args_str = raw_args
        elif isinstance(raw_args, (dict, list)):
            args_str = json.dumps(raw_args, ensure_ascii=False)
        else:
            return None, (
                "Your previous tool call arguments had an unsupported type. "
                + f"The affected tool was `{tool_name or 'unknown'}`. "
                + "Retry the tool call with `function.arguments` as one valid JSON object string."
            )

        new_tc: LLMToolCall = {
            "id": tc.get("id", ""),
            "type": tc.get("type") or "function",
            "function": {
                "name": tool_name,
                "arguments": args_str,
            },
        }
        if "_gemini_extra" in tc:
            new_tc["_gemini_extra"] = tc["_gemini_extra"]
        sanitized.append(new_tc)

    return sanitized, None


# ═══════════════════════════════════════════════════════════════════════════════
# Failover Guard
# ═══════════════════════════════════════════════════════════════════════════════


class FailoverGuard:
    """Guard state for failover decisions."""

    def __init__(self):
        self.tool_executed: bool = False
        self.streaming_started: bool = False
        self.failover_done: bool = False

    def mark_tool_executed(self):
        """Mark that a side-effecting tool has been executed."""
        self.tool_executed = True

    def mark_streaming_started(self):
        """Mark that streaming output has started."""
        self.streaming_started = True

    def mark_failover_done(self):
        """Mark that failover has already happened once."""
        self.failover_done = True

    def can_failover(self) -> bool:
        """Check if failover is allowed based on guard rules."""
        if self.failover_done:
            return False  # Only failover once
        if self.tool_executed:
            return False  # Don't failover after side effects
        return not self.streaming_started  # Don't failover after streaming started


def is_retryable_error(result: str) -> bool:
    """Check if an error result is retryable.
    Uses unified classification from failover.py.
    """
    if not result.startswith(("[LLM Error]", "[LLM call error]", "[Error]")):
        return False

    return classify_error(Exception(result)) != FailoverErrorType.NON_RETRYABLE


def _get_model_timeout(model: LLMModelRecord) -> float:
    """Return the effective request timeout for a model."""
    return float(getattr(model, "request_timeout", None) or 120.0)


def _usage_from_response_or_estimate(response: LLMResponse, api_messages: list[LLMMessage]) -> TokenUsage:
    usage = extract_token_usage(response.usage)
    if usage:
        return usage
    round_chars = sum(len(m.content or "") if isinstance(m.content, str) else 0 for m in api_messages)
    round_chars += len(response.content or "")
    return estimate_token_usage_from_chars(round_chars)


# ═══════════════════════════════════════════════════════════════════════════════
# Helper Functions
# ═══════════════════════════════════════════════════════════════════════════════


async def _get_agent_config(agent_id: uuid.UUID | None, agent: AgentRecord | None = None) -> tuple[int, str | None]:
    """Get agent config: max_tool_rounds and token limit status.

    ``agent`` may supply ``max_tool_rounds``. Token counters are always reloaded
    from the database when ``agent_id`` is set so a long-lived handshake row
    cannot freeze daily/monthly caps.
    """
    if not agent_id and agent is None:
        return 50, None

    try:
        max_rounds = (agent.max_tool_rounds or 50) if agent is not None else 50
        tokens_agent = agent
        if agent_id:
            from app.dao.agent_dao import agent_dao

            fresh = await agent_dao.get(agent_id)
            if fresh is not None:
                tokens_agent = fresh
                if agent is None:
                    max_rounds = fresh.max_tool_rounds or 50
        if tokens_agent:
            if tokens_agent.max_tokens_per_day and tokens_agent.tokens_used_today >= tokens_agent.max_tokens_per_day:
                return (
                    max_rounds,
                    f"⚠️ Daily token usage has reached the limit ({tokens_agent.tokens_used_today:,}/{tokens_agent.max_tokens_per_day:,}). Please try again tomorrow or ask admin to increase the limit.",
                )
            if (
                tokens_agent.max_tokens_per_month
                and tokens_agent.tokens_used_month >= tokens_agent.max_tokens_per_month
            ):
                return (
                    max_rounds,
                    f"⚠️ Monthly token usage has reached the limit ({tokens_agent.tokens_used_month:,}/{tokens_agent.max_tokens_per_month:,}). Please ask admin to increase the limit.",
                )
            return max_rounds, None
    except Exception as exc:
        logger.debug("[LLM] Failed to get agent config for {}: {}", agent_id, exc)
    return 50, None


async def _get_user_name(user_id: uuid.UUID | str | None) -> str | None:
    """Get user's display name for personalized context."""
    resolved_id = as_uuid(user_id)
    if resolved_id is None:
        return None
    try:
        from app.dao.agent_dao import agent_dao
        from app.dao.user_dao import user_dao

        _u = await user_dao.get_with_identity(resolved_id)
        if _u:
            return _u.display_name or _u.username
        # Check Agent name fallback (A2A / agent-as-user paths)
        _a = await agent_dao.get(resolved_id)
        if _a:
            return _a.name
    except Exception as exc:
        logger.debug("[LLM] Failed to get user name for {}: {}", user_id, exc)
    return None


def _convert_messages_for_vision(api_messages: list[LLMMessage], supports_vision: bool) -> list[LLMMessage]:
    """Convert image markers to vision format if supported, or strip them."""
    import copy
    import re as _re_v

    # Deep copy to avoid modifying the original list in place
    new_messages = copy.deepcopy(api_messages)

    if supports_vision:
        # Vision format: convert image markers in strings to OpenAI Vision API list format
        for i, msg in enumerate(new_messages):
            if msg.role != "user" or not msg.content or not isinstance(msg.content, str):
                continue

            content_str = msg.content
            pattern = r"\[image_data:(data:image/[^;]+;base64,[A-Za-z0-9+/=]+)\]"
            images = str_findall(_re_v.compile(pattern), content_str)

            if not images:
                continue

            text = _re_v.sub(pattern, "", content_str).strip()
            parts: list[LLMContentPart] = [{"type": "image_url", "image_url": {"url": img}} for img in images]
            if text:
                # Per OpenAI spec, text part should come after image parts
                parts.append({"type": "text", "text": text})

            new_messages[i] = type(msg)(
                role=msg.role, content=parts, tool_calls=msg.tool_calls, tool_call_id=msg.tool_call_id
            )
    else:
        # Non-vision format: ensure content is a string for all roles, stripping image data.
        _img_marker_pattern = r"\[image_data:data:image/[^;]+;base64,[A-Za-z0-9+/=]+\]"
        for i, msg in enumerate(new_messages):
            if isinstance(msg.content, list):
                # It's a list, join all text parts. This handles user messages
                # with vision content and tool messages from vision_inject.
                text_parts = [part.get("text", "") for part in msg.content if part.get("type") == "text"]
                content_str = "\n".join(text_parts).strip()
                new_messages[i] = type(msg)(
                    role=msg.role, content=content_str, tool_calls=msg.tool_calls, tool_call_id=msg.tool_call_id
                )

            elif isinstance(msg.content, str) and "[image_data:" in msg.content:
                # It's a string with image markers, strip them
                _n_imgs = len(_re_v.findall(_img_marker_pattern, msg.content))
                cleaned = _re_v.sub(_img_marker_pattern, "", msg.content).strip()
                if _n_imgs > 0:
                    cleaned += f"\n[The user sent {_n_imgs} image(s), but the current model does not support vision and cannot view the image content]"
                new_messages[i] = type(msg)(
                    role=msg.role, content=cleaned, tool_calls=msg.tool_calls, tool_call_id=msg.tool_call_id
                )

    return new_messages


def _check_tool_requires_args(tool_name: str, args: ToolPayload) -> tuple[bool, str]:
    """Check if tool requires arguments and return (should_execute, result_or_error)."""
    if not args and tool_name in TOOLS_REQUIRING_ARGS:
        return (
            False,
            f"Error: {tool_name} was called with empty arguments. You must provide the required parameters. Please retry with the correct arguments.",
        )
    return True, ""


def _allowed_tool_names(tools_for_llm: list[ToolDefinition] | None) -> set[str]:
    names: set[str] = set()
    for tool in tools_for_llm or []:
        function = tool.get("function")
        if isinstance(function, dict):
            name = function.get("name")
            if isinstance(name, str) and name.strip():
                names.add(name.strip())
    return names


def _tool_not_enabled_message(tool_name: str) -> str:
    return (
        f"Tool `{tool_name}` is not enabled for this agent. "
        + "Do not call it again. Use only the tools currently available to you, "
        + "or explain that the required capability is not enabled."
    )


async def _process_tool_call(
    tc: LLMToolCall,
    api_messages: list[LLMMessage],
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None,
    session_id: str,
    supports_vision: bool,
    on_tool_call: ToolCallback | None,
    full_reasoning_content: str,
    allowed_tool_names: set[str],
    on_code_output: ToolOutputCallback | None = None,
) -> str:
    """Process a single tool call and return result."""
    fn = tc.get("function") or {}
    tool_name = fn.get("name") or ""
    raw_args = fn.get("arguments", "{}")
    logger.info(f"[LLM] Calling tool: {tool_name}({json.dumps(raw_args, ensure_ascii=False)})")

    if isinstance(raw_args, str):
        try:
            args: ToolPayload = json.loads(raw_args) if raw_args else {}
        except json.JSONDecodeError:
            args = {}
    elif isinstance(raw_args, dict):
        args = raw_args
    else:
        args = {}

    # Guard: check if tool requires arguments
    should_execute, error_msg = _check_tool_requires_args(tool_name, args)
    if not should_execute:
        return error_msg

    # if tool_name not in allowed_tool_names:
    #     result = _tool_not_enabled_message(tool_name)
    #     logger.warning(f"[LLM] Blocked disabled tool call: {tool_name} agent_id={agent_id}")
    #     if on_tool_call:
    #         try:
    #             await on_tool_call({
    #                 "name": tool_name,
    #                 "call_id": tc.get("id", ""),
    #                 "args": args,
    #                 "status": "done",
    #                 "result": result,
    #                 "reasoning_content": full_reasoning_content
    #             })
    #         except Exception:
    #             pass
    #     api_messages.append(LLMMessage(
    #         role="tool",
    #         tool_call_id=tc["id"],
    #         content=result,
    #     ))
    #     return ""

    # Notify client about tool call (in-progress)
    if on_tool_call:
        try:
            callback_data = {
                "name": tool_name,
                "call_id": tc.get("id", ""),
                "args": args,
                "status": "running",
                "reasoning_content": full_reasoning_content,
            }
            await on_tool_call(callback_data)
        except Exception as exc:
            logger.debug("[LLM] Tool callback failed: {}", exc)

    # Execute tool - pass on_output for execute_code streaming
    _on_output: ToolOutputCallback | None = (
        on_code_output if tool_name in ("execute_code", "execute_code_e2b") else None
    )
    result = await execute_tool(
        tool_name,
        args,
        agent_id=agent_id,
        user_id=user_id or agent_id,
        session_id=session_id,
        on_output=_on_output,
    )
    logger.debug(f"[LLM] Tool result: {result[:100]}")

    # ── Vision injection for screenshot tools ──
    tool_content: str | list[LLMContentPart] = str(result)
    if supports_vision and agent_id:
        try:
            from app.services.llm.vision_content import rebuild_llm_content_parts
            from app.services.vision_inject import try_inject_screenshot_vision

            settings = get_settings()
            ws_path = Path(settings.STORAGE_LOCAL_ROOT or settings.AGENT_DATA_DIR) / str(agent_id)
            vision_content = try_inject_screenshot_vision(tool_name, str(result), ws_path)
            if vision_content:
                tool_content = rebuild_llm_content_parts(vision_content)
                logger.info(f"[LLM] Injected screenshot vision for {tool_name}")
        except Exception as e:
            logger.warning(f"[LLM] Vision injection failed for {tool_name}: {e}")

    # Notify client about tool call result
    if on_tool_call:
        try:
            callback_data: ToolCallbackData = {
                "name": tool_name,
                "call_id": tc.get("id", ""),
                "args": args,
                "status": "done",
                "result": result,
                "reasoning_content": full_reasoning_content,
            }
            await on_tool_call(callback_data)
        except Exception as exc:
            logger.debug("[LLM] Tool callback failed: {}", exc)

    api_messages.append(
        LLMMessage(
            role="tool",
            tool_call_id=tc.get("id") or "",
            content=tool_content,
        )
    )
    return ""


# ═══════════════════════════════════════════════════════════════════════════════
# Core LLM Call Functions
# ═══════════════════════════════════════════════════════════════════════════════


async def call_llm(
    model: LLMModelRecord,
    messages: list[OpenAIMessage],
    agent_name: str,
    role_description: str,
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    session_id: str = "",
    on_chunk: ChunkCallback | None = None,
    on_tool_call: ToolCallback | None = None,
    on_tool_delta: ToolCallback | None = None,
    on_thinking: ThinkingCallback | None = None,
    supports_vision: bool = False,
    max_tool_rounds_override: int | None = None,
    skip_tools: bool = False,
    on_code_output: ToolOutputCallback | None = None,
    current_user_name_override: str | None = None,
    system_prompt_suffix: str | None = None,
    turn: TurnContext | None = None,
) -> str:
    """Call LLM via unified client with function-calling tool loop."""
    # Get agent config for tool rounds
    _max_tool_rounds, _token_limit_msg = await _get_agent_config(agent_id, agent=turn.agent if turn else None)
    if _token_limit_msg:
        return _token_limit_msg
    if max_tool_rounds_override and max_tool_rounds_override < _max_tool_rounds:
        _max_tool_rounds = max_tool_rounds_override

    # Get user's name for personalized context
    if current_user_name_override:
        _user_name = current_user_name_override
    elif turn and turn.user_name:
        _user_name = turn.user_name
    elif turn and turn.user is not None:
        _user_name = getattr(turn.user, "display_name", None) or getattr(turn.user, "username", None)
    else:
        _user_name = await _get_user_name(user_id)

    # Auto-assign fallback tool call logger if none provided but conversation context exists
    if on_tool_call is None and session_id:
        from app.services.chat_session_service import save_tool_call_log

        async def _default_on_tool_call(data: ToolCallbackData) -> None:
            if data.get("status") == "done" and agent_id:
                await save_tool_call_log(
                    agent_id=agent_id,
                    user_id=user_id or agent_id,
                    conversation_id=session_id,
                    tool_name=data.get("name", ""),
                    arguments=data.get("args"),
                    result=data.get("result") or "",
                    status="done",
                    tool_call_id=data.get("call_id"),
                    reasoning_content=data.get("reasoning_content"),
                )

        on_tool_call = _default_on_tool_call

    # Build rich prompt with soul, memory, skills, relationships
    from app.services.agent_context import build_agent_context

    # Look up current user's display name so the agent knows who it's talking to
    static_prompt, dynamic_prompt = await build_agent_context(
        agent_id, agent_name, role_description, current_user_name=_user_name
    )
    if system_prompt_suffix:
        dynamic_prompt += system_prompt_suffix

    # Load tools dynamically from DB. `skip_tools=True` is set by the WS
    # handler on the onboarding greeting turn; keep the runtime-level `finish`
    # tool available so every turn still has an explicit stop signal.
    tools_for_llm: list[ToolDefinition]
    if skip_tools:
        tools_for_llm = [FINISH_TOOL_DEFINITION]
    else:
        from app.services.agent_tools_definitions import AGENT_TOOLS

        tools_for_llm = await get_agent_tools_for_llm(agent_id) if agent_id else AGENT_TOOLS
    allowed_tool_names = _allowed_tool_names(tools_for_llm)

    # Convert messages to LLMMessage format
    api_messages: list[LLMMessage] = [LLMMessage(role="system", content=static_prompt, dynamic_content=dynamic_prompt)]
    api_messages.extend(
        LLMMessage(
            role=msg["role"],
            content=msg.get("content"),
            tool_calls=msg.get("tool_calls"),
            tool_call_id=msg.get("tool_call_id"),
        )
        for msg in messages
    )

    # Vision format conversion
    api_messages = _convert_messages_for_vision(api_messages, supports_vision)

    # Create the unified LLM client
    try:
        client = create_llm_client(
            provider=model.provider,
            api_key=get_model_api_key(model),
            model=model.model,
            base_url=model.base_url,
            timeout=_get_model_timeout(model),
        )
    except Exception as e:
        return f"[Error] Failed to create LLM client: {e}"

    max_tokens = get_max_tokens(model.provider, model.model, getattr(model, "max_output_tokens", None))
    _accumulated_usage = TokenUsage()
    _unsaved_usage = TokenUsage()

    # Tool-calling loop
    for round_i in range(_max_tool_rounds):
        # Dynamic tool-call limit warning
        _warn_threshold_80 = int(_max_tool_rounds * 0.8)
        _warn_threshold_96 = _max_tool_rounds - 2
        if round_i == _warn_threshold_80:
            api_messages.append(
                LLMMessage(
                    role="user",
                    content=(
                        f"⚠️ You have used {round_i}/{_max_tool_rounds} tool-call rounds. "
                        + "If the task is incomplete, use upsert_focus_item to save progress "
                        + "and set a continuation trigger with set_trigger before the remaining rounds end."
                    ),
                )
            )
        elif round_i == _warn_threshold_96:
            api_messages.append(
                LLMMessage(
                    role="user",
                    content="🚨 Only 2 tool-call rounds remain. Immediately use upsert_focus_item to save progress and set a continuation trigger.",
                )
            )

        # Check token usage limit mid-loop (every 3 rounds)
        if round_i > 0 and round_i % 3 == 0 and agent_id and _unsaved_usage.total_tokens > 0:
            await record_token_usage(agent_id, _unsaved_usage)
            _unsaved_usage = TokenUsage()
            _, _token_limit_msg = await _get_agent_config(agent_id)
            if _token_limit_msg:
                logger.warning(f"[LLM] Token limit exceeded mid-loop: {_token_limit_msg}")
                await client.close()
                return _token_limit_msg

        try:
            # Use streaming API for real-time responses
            async def _buffer_chunk(_text: str) -> None:
                # Final user-facing text must come through finish(content=...).
                return None

            response = await client.stream(
                messages=api_messages,
                tools=tools_for_llm if tools_for_llm else None,
                temperature=model.temperature,
                max_tokens=max_tokens,
                on_chunk=_buffer_chunk,
                on_tool_delta=on_tool_delta,
                on_thinking=on_thinking,
                llm_provider=model.provider,
                reasoning_effort=getattr(model, "reasoning_effort", None),
            )
        except LLMError as e:
            logger.error(
                f"[LLM] LLMError: provider={getattr(model, 'provider', '?')} model={getattr(model, 'model', '?')} {e}"
            )
            if agent_id and _unsaved_usage.total_tokens > 0:
                await record_token_usage(agent_id, _unsaved_usage)
            await client.close()
            return f"[LLM Error] {e}"
        except Exception as e:
            logger.exception(f"[LLM] Unexpected error: {type(e).__name__}: {str(e)[:300]}")
            if agent_id and _unsaved_usage.total_tokens > 0:
                await record_token_usage(agent_id, _unsaved_usage)
            await client.close()
            return f"[LLM call error] {type(e).__name__}: {str(e)[:200]}"

        # Track tokens for this round
        _usage_this_round = _usage_from_response_or_estimate(response, api_messages)
        _accumulated_usage.add(_usage_this_round)
        _unsaved_usage.add(_usage_this_round)

        # Plain assistant text is not a stop condition. The model must finish
        # explicitly via finish(content=...).
        if not response.tool_calls:
            if response.content:
                api_messages.append(LLMMessage(role="assistant", content=response.content))
            api_messages.append(LLMMessage(role="user", content=FINISH_PROTOCOL_REMINDER))
            continue

        # Execute tool calls
        logger.info(f"[LLM] Round {round_i + 1}: {len(response.tool_calls)} tool call(s)")
        sanitized_tool_calls, retry_instruction = _sanitize_tool_calls_for_context(response.tool_calls)
        if retry_instruction:
            api_messages.append(LLMMessage(role="user", content=retry_instruction))
            continue

        finish_call = find_finish_call(sanitized_tool_calls)
        if finish_call:
            if finish_call.valid:
                if agent_id and _unsaved_usage.total_tokens > 0:
                    await record_token_usage(agent_id, _unsaved_usage)
                await client.close()
                return finish_call.content

            api_messages.append(
                LLMMessage(
                    role="assistant",
                    content=response.content or None,
                    tool_calls=sanitized_tool_calls,
                    reasoning_content=response.reasoning_content,
                )
            )
            api_messages.append(
                LLMMessage(
                    role="tool",
                    content=finish_call.error or "`finish` was invalid.",
                    tool_call_id=finish_call.call_id,
                )
            )
            continue

        # Add assistant message with tool calls
        api_messages.append(
            LLMMessage(
                role="assistant",
                content=response.content or None,
                tool_calls=sanitized_tool_calls,
                reasoning_content=response.reasoning_content,
            )
        )

        full_reasoning_content = response.reasoning_content or ""

        for tc in sanitized_tool_calls or []:
            tool_error = await _process_tool_call(
                tc=tc,
                api_messages=api_messages,
                agent_id=agent_id,
                user_id=user_id,
                session_id=session_id,
                supports_vision=supports_vision,
                on_tool_call=on_tool_call,
                on_code_output=on_code_output,
                full_reasoning_content=full_reasoning_content,
                allowed_tool_names=allowed_tool_names,
            )
            if tool_error:
                api_messages.append(
                    LLMMessage(
                        role="tool",
                        content=tool_error,
                        tool_call_id=tc.get("id", ""),
                    )
                )

    # Record tokens even on "too many rounds" exit
    if agent_id and _unsaved_usage.total_tokens > 0:
        await record_token_usage(agent_id, _unsaved_usage)
    await client.close()
    return "[Error] Too many tool call rounds"


async def call_llm_with_failover(
    primary_model: LLMModelRecord | None,
    fallback_model: LLMModelRecord | None,
    messages: list[OpenAIMessage],
    agent_name: str,
    role_description: str,
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    session_id: str = "",
    on_chunk: ChunkCallback | None = None,
    on_thinking: ThinkingCallback | None = None,
    on_tool_call: ToolCallback | None = None,
    on_tool_delta: ToolCallback | None = None,
    supports_vision: bool = False,
    on_failover: ChunkCallback | None = None,
    skip_tools: bool = False,
    on_code_output: ToolOutputCallback | None = None,
    current_user_name_override: str | None = None,
    system_prompt_suffix: str | None = None,
    turn: TurnContext | None = None,
) -> str:
    """Call LLM with automatic failover support."""
    guard = FailoverGuard()

    # Config-level fallback: if no primary, use fallback directly
    if primary_model is None and fallback_model is not None:
        logger.info("[Failover] Primary model not configured, using fallback directly")
        primary_model = fallback_model
        fallback_model = None

    if primary_model is None:
        return "⚠️ No LLM model configured"

    # Wrapper callbacks to track state for guard checks
    async def _wrapped_on_chunk(text: str):
        guard.mark_streaming_started()
        if on_chunk:
            await on_chunk(text)

    async def _wrapped_on_tool_call(data: ToolCallbackData) -> None:
        if data.get("status") == "done":
            guard.mark_tool_executed()
        if on_tool_call:
            await on_tool_call(data)

    # Try primary model
    primary_result = await call_llm(
        primary_model,
        messages,
        agent_name,
        role_description,
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        on_chunk=_wrapped_on_chunk,
        on_tool_call=_wrapped_on_tool_call,
        on_tool_delta=on_tool_delta,
        on_thinking=on_thinking,
        supports_vision=supports_vision,
        skip_tools=skip_tools,
        on_code_output=on_code_output,
        current_user_name_override=current_user_name_override,
        system_prompt_suffix=system_prompt_suffix,
        turn=turn,
    )

    # Check if we need to failover
    if not is_retryable_error(primary_result):
        logger.warning(f"[Failover] Canceled: Primary model returned a non-retryable error: {primary_result[:150]}")
        return primary_result

    # Check guard conditions
    if not guard.can_failover():
        if guard.tool_executed:
            logger.warning("[Failover] Blocked: side-effecting tool already executed")
        elif guard.streaming_started:
            logger.warning("[Failover] Blocked: streaming already started")
        elif guard.failover_done:
            logger.warning("[Failover] Blocked: failover already done once")
        return primary_result

    # No fallback available
    if fallback_model is None:
        logger.warning("[Failover] No fallback model available")
        return primary_result

    # Runtime failover: retry with fallback model
    logger.info(f"[Failover] Retrying with fallback model: {fallback_model.provider}/{fallback_model.model}")

    if on_failover:
        try:
            await on_failover("Switched to a backup model")
        except Exception as exc:
            logger.debug("[Failover] Callback failed: {}", exc)

    guard.mark_failover_done()

    # Call fallback with fresh callbacks
    fallback_guard = FailoverGuard()
    fallback_guard.mark_failover_done()

    async def _fallback_on_chunk(text: str):
        fallback_guard.mark_streaming_started()
        if on_chunk:
            await on_chunk(text)

    async def _fallback_on_tool_call(data: ToolCallbackData) -> None:
        if data.get("status") == "done":
            fallback_guard.mark_tool_executed()
        if on_tool_call:
            await on_tool_call(data)

    fallback_result = await call_llm(
        fallback_model,
        messages,
        agent_name,
        role_description,
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        on_chunk=_fallback_on_chunk,
        on_tool_call=_fallback_on_tool_call,
        on_tool_delta=on_tool_delta,
        on_thinking=on_thinking,
        supports_vision=getattr(fallback_model, "supports_vision", False),
        skip_tools=skip_tools,
        on_code_output=on_code_output,
        current_user_name_override=current_user_name_override,
        system_prompt_suffix=system_prompt_suffix,
        turn=turn,
    )

    # Combine error messages if fallback also failed
    if is_retryable_error(fallback_result) or fallback_result.startswith(("⚠️", "[Error]")):
        return f"⚠️ Model call failed: Primary: {primary_result[:80]} | Fallback: {fallback_result[:80]}"

    return fallback_result


# ═══════════════════════════════════════════════════════════════════════════════
# High-level Agent Call Functions
# ═══════════════════════════════════════════════════════════════════════════════


async def call_agent_llm(
    db: object,  # dual-stack: accepted for call-site compatibility; DAO path ignores SQLAlchemy session
    agent_id: uuid.UUID,
    user_text: str,
    history: list[OpenAIMessage] | None = None,
    user_id: uuid.UUID | None = None,
    session_id: str = "",
    on_chunk: ChunkCallback | None = None,
    on_thinking: ThinkingCallback | None = None,
    supports_vision: bool = False,
    turn: TurnContext | None = None,
) -> str:
    """Call the agent's LLM with automatic failover support."""
    from app.core.permissions import is_agent_expired
    from app.dao.agent_dao import agent_dao

    agent = turn.agent if turn and turn.agent is not None else await agent_dao.get(agent_id)
    if not agent:
        return "⚠️ Digital employee not found"

    if is_agent_expired(agent):
        return "This Agent has expired and is off duty. Please contact your admin to extend its service."

    from app.services.llm.router import load_agent_model_bundle, select_turn_model

    bundle = await load_agent_model_bundle(
        agent,
        primary=turn.primary_model if turn else None,
        secondary=turn.secondary_model if turn else None,
        fallback=turn.fallback_model if turn else None,
    )

    # Build conversation messages
    messages: list[OpenAIMessage] = []
    if history:
        messages.extend(history[-10:])
    messages.append({"role": "user", "content": user_text})

    choice = await select_turn_model(
        bundle,
        user_text=user_text,
        history=history,
        agent_id=agent_id,
    )
    selected_model = choice.model
    failover_model = choice.failover_model
    selected_slot = choice.slot
    complexity = choice.complexity
    routing_reason = choice.reason

    if not selected_model:
        return f"⚠️ {agent.name} has no LLM model configured. Configure one in the admin console."

    routed_turn = turn or TurnContext(
        agent=agent,
        primary_model=bundle.primary,
        secondary_model=bundle.secondary,
        fallback_model=bundle.fallback,
    )
    routed_turn.selected_model = selected_model
    routed_turn.selected_slot = selected_slot
    routed_turn.complexity = complexity
    routed_turn.routing_reason = routing_reason

    # Use unified call_llm_with_failover
    try:
        return await call_llm_with_failover(
            primary_model=selected_model,
            fallback_model=failover_model,
            messages=messages,
            agent_name=agent.name,
            role_description=agent.role_description or "",
            agent_id=agent_id,
            user_id=user_id or agent_id,
            session_id=session_id,
            on_chunk=on_chunk,
            on_thinking=on_thinking,
            supports_vision=supports_vision or getattr(selected_model, "supports_vision", False),
            turn=routed_turn,
        )
    except Exception as e:
        error_msg = str(e) or repr(e)
        logger.error(f"[call_agent_llm] Unexpected error: {error_msg}")
        return f"⚠️ Model call failed: {error_msg[:150]}"


async def call_agent_llm_with_tools(
    db: object,  # dual-stack: accepted for call-site compatibility; DAO path ignores SQLAlchemy session
    agent_id: uuid.UUID,
    system_prompt: str,
    user_prompt: str,
    max_rounds: int = 50,
    session_id: str = "",
    turn: TurnContext | None = None,
) -> str:
    """Call agent LLM with tool-calling loop (for background services)."""
    from app.dao.agent_dao import agent_dao
    from app.dao.llm_dao import llm_model_dao

    agent = turn.agent if turn and turn.agent is not None else await agent_dao.get(agent_id)
    if not agent:
        return "⚠️ Agent not found"

    primary_model = turn.primary_model if turn else None
    fallback_model = turn.fallback_model if turn else None
    missing_ids = [
        mid
        for mid, loaded in (
            (agent.primary_model_id, primary_model),
            (agent.fallback_model_id, fallback_model),
        )
        if mid and loaded is None
    ]
    if missing_ids:
        loaded = {row.id: row for row in await llm_model_dao.get_many(missing_ids)}
        if primary_model is None and agent.primary_model_id:
            primary_model = loaded.get(agent.primary_model_id)
        if fallback_model is None and agent.fallback_model_id:
            fallback_model = loaded.get(agent.fallback_model_id)

    # Config-level fallback
    if not primary_model and fallback_model:
        primary_model = fallback_model
        fallback_model = None

    if not primary_model:
        return f"⚠️ {agent.name} has no LLM model configured"

    # Build messages
    messages = [
        LLMMessage(role="system", content=system_prompt),
        LLMMessage(role="user", content=user_prompt),
    ]

    tools_for_llm = await get_agent_tools_for_llm(agent_id)
    allowed_tool_names = _allowed_tool_names(tools_for_llm)

    async def _try_model(model: LLMModelRecord) -> tuple[str, bool, bool]:
        """Try to complete with a model. Returns (response, success, tool_executed)."""
        _accumulated_usage = TokenUsage()
        _unsaved_usage = TokenUsage()
        tool_executed = False
        try:
            client = create_llm_client(
                provider=model.provider,
                api_key=get_model_api_key(model),
                model=model.model,
                base_url=model.base_url,
                timeout=_get_model_timeout(model),
            )

            max_tokens = get_max_tokens(model.provider, model.model, getattr(model, "max_output_tokens", None))

            # Tool-calling loop
            api_messages = list(messages)
            for round_i in range(max_rounds):
                # Check token usage limit mid-loop (every 3 rounds)
                if round_i > 0 and round_i % 3 == 0 and agent_id and _unsaved_usage.total_tokens > 0:
                    await record_token_usage(agent_id, _unsaved_usage)
                    _unsaved_usage = TokenUsage()
                    _, _token_limit_msg = await _get_agent_config(agent_id)
                    if _token_limit_msg:
                        logger.warning(f"[call_agent_llm_with_tools] Token limit exceeded mid-loop: {_token_limit_msg}")
                        await client.close()
                        return _token_limit_msg, False, tool_executed

                try:
                    response = await client.complete(
                        messages=api_messages,
                        tools=tools_for_llm if tools_for_llm else None,
                        temperature=model.temperature,
                        max_tokens=max_tokens,
                        llm_provider=model.provider,
                        reasoning_effort=getattr(model, "reasoning_effort", None),
                    )
                except Exception as e:
                    logger.error(f"[call_agent_llm_with_tools] Agent {agent_id}: LLM call error: {e}")
                    await client.close()
                    if agent_id and _unsaved_usage.total_tokens > 0:
                        await record_token_usage(agent_id, _unsaved_usage)
                    raise

                # Track tokens for this round
                _usage_this_round = _usage_from_response_or_estimate(response, api_messages)
                _accumulated_usage.add(_usage_this_round)
                _unsaved_usage.add(_usage_this_round)

                if not response.tool_calls:
                    if response.content:
                        api_messages.append(LLMMessage(role="assistant", content=response.content))
                    api_messages.append(LLMMessage(role="user", content=FINISH_PROTOCOL_REMINDER))
                    continue

                # Execute tool calls
                sanitized_tool_calls, retry_instruction = _sanitize_tool_calls_for_context(response.tool_calls)
                if retry_instruction:
                    api_messages.append(LLMMessage(role="user", content=retry_instruction))
                    continue

                finish_call = find_finish_call(sanitized_tool_calls)
                if finish_call:
                    if finish_call.valid:
                        if agent_id and _unsaved_usage.total_tokens > 0:
                            await record_token_usage(agent_id, _unsaved_usage)
                        await client.close()
                        return finish_call.content, True, tool_executed
                    api_messages.append(
                        LLMMessage(
                            role="assistant",
                            content=response.content or None,
                            tool_calls=sanitized_tool_calls,
                            reasoning_content=response.reasoning_content,
                        )
                    )
                    api_messages.append(
                        LLMMessage(
                            role="tool",
                            tool_call_id=finish_call.call_id,
                            content=finish_call.error or "`finish` was invalid.",
                        )
                    )
                    continue

                api_messages.append(
                    LLMMessage(
                        role="assistant",
                        content=response.content or None,
                        tool_calls=sanitized_tool_calls,
                        reasoning_content=response.reasoning_content,
                    )
                )

                for tc in sanitized_tool_calls or []:
                    fn = tc.get("function") or {}
                    tool_name = fn.get("name") or ""
                    raw_args = fn.get("arguments", "{}")
                    try:
                        args = parse_tool_arguments(raw_args)
                    except json.JSONDecodeError:
                        args = {}

                    tool_executed = True
                    if tool_name not in allowed_tool_names:
                        logger.warning(
                            f"[call_agent_llm_with_tools] Blocked disabled tool call: {tool_name} agent_id={agent_id}"
                        )
                        result = _tool_not_enabled_message(tool_name)
                    else:
                        result = await execute_tool(
                            tool_name,
                            args,
                            agent_id=agent_id,
                            user_id=agent.creator_id,
                            session_id=session_id,
                        )
                    api_messages.append(
                        LLMMessage(
                            role="tool",
                            tool_call_id=tc.get("id") or "",
                            content=str(result),
                        )
                    )

            if agent_id and _unsaved_usage.total_tokens > 0:
                await record_token_usage(agent_id, _unsaved_usage)
            await client.close()
            return "[Error] Too many tool call rounds", False, tool_executed

        except Exception as e:
            if agent_id and _unsaved_usage.total_tokens > 0:
                await record_token_usage(agent_id, _unsaved_usage)
            return f"[Error] {e}", False, tool_executed

    # Try primary model
    reply, success, primary_tool_executed = await _try_model(primary_model)
    if success:
        return reply

    # Primary failed - check if retryable
    error_type = classify_error(Exception(reply))
    if error_type == FailoverErrorType.NON_RETRYABLE or not fallback_model:
        return reply

    if primary_tool_executed:
        logger.warning("[call_agent_llm_with_tools] Blocked fallback: side-effecting tool already executed")
        return reply

    # Try fallback model
    logger.info(f"[call_agent_llm_with_tools] Retrying with fallback: {fallback_model.model}")
    reply2, success2, _fallback_tool_executed = await _try_model(fallback_model)
    if success2:
        return reply2

    return f"⚠️ Both models failed | Primary: {reply[:80]} | Fallback: {reply2[:80]}"


__all__ = [
    "FailoverGuard",
    "call_agent_llm",
    "call_agent_llm_with_tools",
    "call_llm",
    "call_llm_with_failover",
    "is_retryable_error",
]
