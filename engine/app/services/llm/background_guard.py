"""Ownership fencing and explicit outcomes for background tool loops."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from app.core.logging import logger
from app.records.agent import AgentRecord
from app.records.llm import LLMModelRecord
from app.services.quota_guard import QuotaExceeded, consume_agent_llm_call
from app.services.token_tracker import TokenUsage

from .base import ToolDefinition
from .finish import FINISH_PROTOCOL_REMINDER, find_finish_call, parse_tool_arguments
from .types import LLMMessage


class ScheduleLeaseLostError(Exception):
    """The background caller no longer owns its scheduled occurrence."""


class BackgroundCallGuard(Protocol):
    def ensure_held(self) -> None:
        """Reject dispatch after locally observed ownership loss."""
        ...

    async def confirm(self) -> None:
        """Actively confirm database ownership or raise ScheduleLeaseLostError."""
        ...


@dataclass(frozen=True, slots=True)
class BackgroundCallOutcome:
    status: Literal["success", "failure"]
    content: str


@dataclass(frozen=True, slots=True)
class BackgroundToolLoop:
    agent_id: UUID
    agent: AgentRecord
    messages: list[LLMMessage]
    tools: list[ToolDefinition]
    max_rounds: int
    session_id: str
    guard: BackgroundCallGuard | None

    def ensure_held(self) -> None:
        if self.guard is not None:
            self.guard.ensure_held()

    async def confirm(self) -> None:
        if self.guard is not None:
            await self.guard.confirm()

    async def try_model(self, model: LLMModelRecord) -> tuple[str, bool, bool]:
        from . import caller

        unsaved_usage = TokenUsage()
        tool_executed = False

        async def flush_usage() -> None:
            nonlocal unsaved_usage
            if unsaved_usage.total_tokens > 0:
                await self.confirm()
                usage, unsaved_usage = unsaved_usage, TokenUsage()
                await caller.record_token_usage(self.agent_id, usage)
                await self.confirm()

        async def failure(error: Exception) -> tuple[str, bool, bool]:
            await self.confirm()
            await flush_usage()
            logger.error(f"[call_agent_llm_with_tools] Agent {self.agent_id}: LLM call error: {error}")
            return f"[Error] {error}", False, tool_executed

        self.ensure_held()
        try:
            model, client = await caller.create_fresh_llm_client_from_model(
                model, timeout=caller._get_model_timeout(model)
            )
        except ScheduleLeaseLostError:
            raise
        except Exception as error:
            return await failure(error)
        try:
            await self.confirm()
            max_tokens = caller.get_max_tokens(model.provider, model.model, model.max_output_tokens)
            api_messages = list(self.messages)
            allowed_tool_names = caller._allowed_tool_names(self.tools)
            for round_index in range(self.max_rounds):
                if round_index > 0 and round_index % 3 == 0 and unsaved_usage.total_tokens > 0:
                    await flush_usage()
                    _, token_limit_message = await caller._get_agent_config(self.agent_id)
                    await self.confirm()
                    if token_limit_message not in (None, ""):
                        logger.warning(
                            f"[call_agent_llm_with_tools] Token limit exceeded mid-loop: {token_limit_message}"
                        )
                        return token_limit_message, False, tool_executed

                self.ensure_held()
                try:
                    await consume_agent_llm_call(self.agent_id)
                except QuotaExceeded:
                    await flush_usage()
                    raise
                self.ensure_held()
                try:
                    response = await client.complete(
                        messages=api_messages,
                        tools=self.tools or None,
                        temperature=model.temperature,
                        max_tokens=max_tokens,
                        llm_provider=model.provider,
                        reasoning_effort=model.reasoning_effort,
                    )
                except ScheduleLeaseLostError:
                    raise
                except Exception as error:
                    return await failure(error)
                await self.confirm()
                unsaved_usage.add(caller._usage_from_response_or_estimate(response, api_messages))
                if not response.tool_calls:
                    if response.content:
                        api_messages.append(LLMMessage(role="assistant", content=response.content))
                    api_messages.append(LLMMessage(role="user", content=FINISH_PROTOCOL_REMINDER))
                    continue

                tool_calls, retry_instruction = caller._sanitize_tool_calls_for_context(response.tool_calls)
                if retry_instruction not in (None, ""):
                    api_messages.append(LLMMessage(role="user", content=retry_instruction))
                    continue
                finish_call = find_finish_call(tool_calls)
                if finish_call and finish_call.valid:
                    await flush_usage()
                    return finish_call.content, True, tool_executed
                api_messages.append(
                    LLMMessage(
                        role="assistant",
                        content=response.content or None,
                        tool_calls=tool_calls,
                        reasoning_content=response.reasoning_content,
                        provider_items=response.provider_items,
                    )
                )
                if finish_call:
                    api_messages.append(
                        LLMMessage(
                            role="tool",
                            tool_call_id=finish_call.call_id,
                            content=finish_call.error or "`finish` was invalid.",
                        )
                    )
                    continue

                for tool_call in tool_calls or []:
                    function = tool_call.get("function") or {}
                    tool_name = function.get("name") or ""
                    try:
                        arguments = parse_tool_arguments(function.get("arguments", "{}"))
                    except json.JSONDecodeError:
                        arguments = {}
                    self.ensure_held()
                    tool_executed = True
                    if tool_name not in allowed_tool_names:
                        logger.warning(
                            f"[call_agent_llm_with_tools] Blocked disabled tool call: {tool_name} agent_id={self.agent_id}"
                        )
                        result = caller._tool_not_enabled_message(tool_name)
                    else:
                        try:
                            result = await caller.execute_tool(
                                tool_name,
                                arguments,
                                agent_id=self.agent_id,
                                user_id=self.agent.creator_id,
                                session_id=self.session_id,
                            )
                        except ScheduleLeaseLostError:
                            raise
                        except Exception as error:
                            return await failure(error)
                        await self.confirm()
                    api_messages.append(
                        LLMMessage(role="tool", tool_call_id=tool_call.get("id") or "", content=str(result))
                    )

            await flush_usage()
            return "[Error] Too many tool call rounds", False, tool_executed
        finally:
            await client.close()
