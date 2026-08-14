"""Anthropic native LLM provider client."""

from __future__ import annotations

import json
from typing import Any, override, ClassVar

import httpx

from app.core.json_types import (
    JsonObject,
    is_json_object,
    is_str_dict,
    json_as_int,
    json_as_str,
    json_as_str_or,
    json_object_from,
)
from app.services.llm.base import (
    ChunkCallback,
    LLMClient,
    LLMError,
    ThinkingCallback,
    ToolCallback,
    ToolCallbackData,
    ToolDefinition,
)
from app.services.llm.types import LLMMessage, LLMResponse, LLMToolCall, LLMUsage


def _usage_from_json(value: JsonObject) -> LLMUsage:
    usage: LLMUsage = {}
    for key, raw in value.items():
        if isinstance(raw, int) and not isinstance(raw, bool):
            usage[key] = raw
    return usage


class AnthropicClient(LLMClient):
    """Client for Anthropic's native Messages API.

    Supports Claude 3.x and Claude 3.7+ with extended thinking.
    """

    DEFAULT_BASE_URL: ClassVar[str] = "https://api.anthropic.com"
    API_VERSION: ClassVar[str] = "2023-06-01"

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 120.0,
    ):
        super().__init__(api_key, base_url or self.DEFAULT_BASE_URL, model, timeout)
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout, follow_redirects=True, proxy=None)
        return self._client

    @override
    def _get_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": self.API_VERSION,
            "anthropic-beta": "prompt-caching-2024-07-31",
        }

    def _normalize_base_url(self) -> str:
        """Normalize base URL by stripping trailing API paths."""
        url = (self.base_url or self.DEFAULT_BASE_URL).rstrip("/")
        if url.endswith("/v1/messages"):
            url = url[: -len("/v1/messages")]
        elif url.endswith("/v1/chat/completions"):
            url = url[: -len("/v1/chat/completions")]
        elif url.endswith("/v1"):
            url = url[: -len("/v1")]
        return url

    def _build_payload(
        self,
        messages: list[LLMMessage],
        tools: list[ToolDefinition] | None,
        temperature: float | None,
        max_tokens: int | None,
        stream: bool = False,
        **kwargs: object,
    ) -> dict[str, object]:
        """Build Anthropic request payload."""
        system_blocks: list[dict[str, object]] = []
        anthropic_messages: list[dict[str, object]] = []

        for msg in messages:
            if msg.role == "system":
                if msg.content:
                    system_blocks.append({"type": "text", "text": msg.content, "cache_control": {"type": "ephemeral"}})
                if msg.dynamic_content:
                    system_blocks.append({"type": "text", "text": f"\n{msg.dynamic_content}"})
            else:
                formatted = msg.to_anthropic_format()
                if formatted:
                    anthropic_messages.append({"role": formatted["role"], "content": formatted["content"]})

        # In Anthropic prompt caching, we also want to cache_control the last user message
        # So we add cache_control to the very last message in the history if it's a user message
        if anthropic_messages and anthropic_messages[-1]["role"] == "user":
            user_msg = anthropic_messages[-1]
            content = user_msg["content"]
            if isinstance(content, list) and content:
                # Ensure the last block of the user message has cache_control
                blocks: list[dict[str, object]] = [dict(block) for block in content if is_str_dict(block)]
                if blocks:
                    blocks[-1]["cache_control"] = {"type": "ephemeral"}
                    user_msg["content"] = blocks
            elif isinstance(content, str):
                user_msg["content"] = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]

        payload: dict[str, object] = {
            "model": self.model,
            "messages": anthropic_messages,
            "max_tokens": max_tokens or 4096,
            "stream": stream,
        }
        if temperature is not None:
            payload["temperature"] = temperature

        if system_blocks:
            payload["system"] = system_blocks

        # Handle Extended Thinking
        thinking = kwargs.pop("thinking", None)
        if thinking:
            payload["thinking"] = thinking
            # For thinking models, temperature must be 1.0 or omitted in some cases
            # But usually it's best to let user specify or default to 1.0 if not set
            if "temperature" not in kwargs:
                payload["temperature"] = 1.0

        if tools:
            anthropic_tools: list[dict[str, object]] = []
            for tool in tools:
                function = tool.get("function")
                if tool.get("type") == "function" and isinstance(function, dict):
                    anthropic_tools.append(
                        {
                            "name": function.get("name", ""),
                            "description": function.get("description", ""),
                            "input_schema": function.get("parameters", {"type": "object"}),
                        }
                    )
            if anthropic_tools:
                anthropic_tools[-1]["cache_control"] = {"type": "ephemeral"}
            payload["tools"] = anthropic_tools

        payload.update(kwargs)
        return payload

    @override
    async def complete(
        self,
        messages: list[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Non-streaming completion."""
        url = f"{self._normalize_base_url()}/v1/messages"
        payload = self._build_payload(messages, tools, temperature, max_tokens, stream=False, **kwargs)

        client = await self._get_client()
        response = await client.post(url, json=payload, headers=self._get_headers())

        if response.status_code >= 400:
            error_text = response.text[:500]
            raise LLMError(f"HTTP {response.status_code}: {error_text}")

        data = json_object_from(response.json())
        if data.get("type") == "error":
            raise LLMError(f"API error: {data.get('error', {})}")

        full_content = ""
        full_reasoning = ""
        full_signature: str | None = None
        tool_calls: list[LLMToolCall] = []

        content_blocks = data.get("content", [])
        for block in content_blocks if isinstance(content_blocks, list) else []:
            if not is_json_object(block):
                continue
            block_type = block.get("type")
            if block_type == "text":
                full_content += json_as_str_or(block.get("text"))
            elif block_type == "thinking":
                full_reasoning += json_as_str_or(block.get("thinking"))
                signature = json_as_str(block.get("signature"))
                if signature is not None:
                    full_signature = signature
            elif block_type == "tool_use":
                tool_calls.append(
                    {
                        "id": json_as_str_or(block.get("id")),
                        "type": "function",
                        "function": {
                            "name": json_as_str_or(block.get("name")),
                            "arguments": json.dumps(json_object_from(block.get("input")), ensure_ascii=False),
                        },
                    }
                )

        usage = None
        usage_raw = data.get("usage")
        if is_json_object(usage_raw):
            usage = {
                "input_tokens": json_as_int(usage_raw.get("input_tokens")),
                "output_tokens": json_as_int(usage_raw.get("output_tokens")),
                "cache_creation_input_tokens": json_as_int(usage_raw.get("cache_creation_input_tokens")),
                "cache_read_input_tokens": json_as_int(usage_raw.get("cache_read_input_tokens")),
            }

        return LLMResponse(
            content=full_content,
            tool_calls=tool_calls,
            reasoning_content=full_reasoning or None,
            reasoning_signature=full_signature,
            finish_reason=json_as_str(data.get("stop_reason")),
            usage=usage,
            model=json_as_str(data.get("model")),
        )

    @override
    async def stream(
        self,
        messages: list[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        on_chunk: ChunkCallback | None = None,
        on_tool_delta: ToolCallback | None = None,
        on_thinking: ThinkingCallback | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Streaming completion."""
        url = f"{self._normalize_base_url()}/v1/messages"
        payload = self._build_payload(messages, tools, temperature, max_tokens, stream=True, **kwargs)

        full_content = ""
        full_reasoning = ""
        full_signature: str | None = None
        tool_calls_data: list[LLMToolCall] = []
        tool_call_index_map: dict[int, int] = {}
        last_finish_reason: str | None = None
        final_usage: LLMUsage | None = None
        final_model = self.model

        client = await self._get_client()

        try:
            async with client.stream("POST", url, json=payload, headers=self._get_headers()) as resp:
                if resp.status_code >= 400:
                    error_body = ""
                    async for chunk in resp.aiter_bytes():
                        error_body += chunk.decode(errors="replace")
                    raise LLMError(f"HTTP {resp.status_code}: {error_body[:500]}")

                current_event = None

                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue

                    if line.startswith("event:"):
                        current_event = line[len("event:") :].strip()
                        continue

                    if not line.startswith("data:"):
                        continue

                    data_str = line[len("data:") :].strip()
                    if data_str == "[DONE]":
                        break

                    try:
                        data = json_object_from(json.loads(data_str))
                    except json.JSONDecodeError:
                        continue

                    # Handle events
                    if current_event == "message_start":
                        msg = json_object_from(data.get("message"))
                        model_name = json_as_str(msg.get("model"))
                        if model_name:
                            final_model = model_name
                        usage_raw = msg.get("usage")
                        if is_json_object(usage_raw):
                            final_usage = _usage_from_json(usage_raw)

                    elif current_event == "content_block_start":
                        block = json_object_from(data.get("content_block"))
                        idx = json_as_int(data.get("index"))
                        if block.get("type") == "tool_use":
                            tool_call_index_map[idx] = len(tool_calls_data)
                            tool_calls_data.append(
                                {
                                    "id": json_as_str_or(block.get("id")),
                                    "type": "function",
                                    "function": {"name": json_as_str_or(block.get("name")), "arguments": ""},
                                }
                            )
                            if on_tool_delta:
                                callback_data: ToolCallbackData = {
                                    "id": json_as_str_or(block.get("id"), f"draft-{idx}"),
                                    "index": idx,
                                    "name": json_as_str_or(block.get("name")),
                                    "arguments": "",
                                }
                                await on_tool_delta(callback_data)

                    elif current_event == "content_block_delta":
                        idx = json_as_int(data.get("index"))
                        delta = json_object_from(data.get("delta"))
                        delta_type = delta.get("type")

                        if delta_type == "text_delta":
                            text = json_as_str_or(delta.get("text"))
                            full_content += text
                            if on_chunk:
                                await on_chunk(text)

                        elif delta_type == "thinking_delta":
                            thought = json_as_str_or(delta.get("thinking"))
                            full_reasoning += thought
                            if on_thinking:
                                await on_thinking(thought)

                        elif delta_type == "signature_delta":
                            signature = json_as_str(delta.get("signature"))
                            if signature is not None:
                                full_signature = signature

                        elif delta_type == "input_json_delta":
                            if idx in tool_call_index_map:
                                tc_idx = tool_call_index_map[idx]
                                tool_call = tool_calls_data[tc_idx]
                                function = tool_call.get("function") or {}
                                tool_name = function.get("name") or ""
                                arguments = function.get("arguments")
                                current_arguments = arguments if isinstance(arguments, str) else ""
                                partial_json = delta.get("partial_json", "")
                                updated_arguments = current_arguments + (
                                    partial_json if isinstance(partial_json, str) else ""
                                )
                                tool_call["function"] = {"name": tool_name, "arguments": updated_arguments}
                                if on_tool_delta:
                                    callback_data = {
                                        "id": tool_call.get("id") or f"draft-{idx}",
                                        "index": idx,
                                        "name": tool_name,
                                        "arguments": updated_arguments,
                                    }
                                    await on_tool_delta(callback_data)

                    elif current_event == "message_delta":
                        delta = json_object_from(data.get("delta"))
                        stop_reason = json_as_str(delta.get("stop_reason"))
                        if stop_reason:
                            last_finish_reason = stop_reason
                        usage_raw = data.get("usage")
                        if is_json_object(usage_raw):
                            # message_delta usage is cumulative
                            final_usage = _usage_from_json(usage_raw)

                    elif current_event == "error":
                        error_info = json_object_from(data.get("error"))
                        raise LLMError(
                            f"Anthropic stream error ({error_info.get('type')}): {error_info.get('message')}"
                        )

                    elif current_event == "message_stop":
                        break

        except (httpx.ConnectError, httpx.ReadError, httpx.ConnectTimeout) as e:
            raise LLMError(f"Connection failed: {e}") from e

        # Normalize stop reason to OpenAI style (optional but helpful for consistency)
        if last_finish_reason == "end_turn":
            last_finish_reason = "stop"
        elif last_finish_reason == "tool_use":
            last_finish_reason = "tool_calls"

        return LLMResponse(
            content=full_content,
            tool_calls=tool_calls_data,
            reasoning_content=full_reasoning or None,
            reasoning_signature=full_signature,
            finish_reason=last_finish_reason,
            usage=final_usage,
            model=final_model,
        )

    @override
    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
