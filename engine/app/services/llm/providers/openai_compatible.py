"""OpenAI-compatible LLM provider client."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, override

import httpx

from app.core.logging import logger
from app.services.llm.base import (
    ChunkCallback,
    LLMClient,
    LLMError,
    ThinkingCallback,
    ToolCallback,
    ToolCallbackData,
    ToolDefinition,
)
from app.services.llm.types import LLMMessage, LLMResponse, LLMStreamChunk, LLMToolCall


class OpenAICompatibleClient(LLMClient):
    """Client for OpenAI-compatible APIs (OpenAI, DeepSeek, Qwen, etc.)."""

    base_url: str
    DEFAULT_BASE_URL = "https://api.openai.com/v1"

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 120.0,
        supports_tool_choice: bool = True,
        supports_cache_control: bool = False,
    ):
        super().__init__(api_key, base_url or self.DEFAULT_BASE_URL, model, timeout)
        self.supports_tool_choice = supports_tool_choice
        self.supports_cache_control = supports_cache_control
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
            "Authorization": f"Bearer {self.api_key}",
        }

    def _normalize_base_url(self) -> str:
        """Normalize base URL by stripping trailing /chat/completions."""
        url = self.base_url.rstrip("/")
        if url.endswith("/chat/completions"):
            url = url[: -len("/chat/completions")]
        return url

    def _build_payload(
        self,
        messages: list[LLMMessage],
        tools: list[ToolDefinition] | None,
        temperature: float | None,
        max_tokens: int | None,
        stream: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Build request payload."""
        messages_payload = self._messages_to_openai_payload(messages)
        logger.debug(
            f"[LLM-Debug] OpenAICompatibleClient payload messages for model {self.model}: {json.dumps(messages_payload, indent=2, ensure_ascii=False)}"
        )
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages_payload,
            "stream": stream,
        }
        if temperature is not None:
            payload["temperature"] = temperature

        # Request usage stats in streaming responses (OpenAI extension)
        if stream:
            payload["stream_options"] = {"include_usage": True}

        if max_tokens:
            payload["max_tokens"] = max_tokens

        if tools:
            payload["tools"] = tools
            if self.supports_tool_choice:
                payload["tool_choice"] = "auto"
                payload["parallel_tool_calls"] = True

        # Add any additional kwargs
        payload.update(kwargs)

        return payload

    def _messages_to_openai_payload(self, messages: list[LLMMessage]) -> list[dict[str, object]]:
        """Convert messages, optionally adding DashScope/OpenAI-compatible cache hints."""
        if not self.supports_cache_control:
            return [dict(message.to_openai_format()) for message in messages]

        payload: list[dict[str, object]] = []
        last_user_index = -1

        for msg in messages:
            if msg.role == "system":
                formatted: dict[str, object] = {"role": "system"}
                content_blocks: list[dict[str, object]] = []

                if isinstance(msg.content, str) and msg.content:
                    content_blocks.append(
                        {
                            "type": "text",
                            "text": msg.content,
                            "cache_control": {"type": "ephemeral"},
                        }
                    )
                elif isinstance(msg.content, list):
                    content_blocks = [dict(part) for part in msg.content if isinstance(part, dict)]
                    self._mark_last_text_block_cacheable(content_blocks)

                if msg.dynamic_content:
                    content_blocks.append(
                        {
                            "type": "text",
                            "text": f"\n\n{msg.dynamic_content}",
                        }
                    )

                if content_blocks:
                    formatted["content"] = content_blocks
                if msg.tool_calls:
                    formatted["tool_calls"] = msg.tool_calls
                payload.append(formatted)
                continue

            payload.append(dict(msg.to_openai_format()))
            if msg.role == "user":
                last_user_index = len(payload) - 1

        if last_user_index >= 0:
            payload[last_user_index] = self._with_cache_control_on_message(payload[last_user_index])

        return payload

    def _with_cache_control_on_message(self, message: dict[str, object]) -> dict[str, object]:
        content = message.get("content")
        if isinstance(content, str) and content:
            message = dict(message)
            message["content"] = [
                {
                    "type": "text",
                    "text": content,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
            return message
        if isinstance(content, list):
            blocks = [dict(part) for part in content if isinstance(part, dict)]
            if self._mark_last_text_block_cacheable(blocks):
                message = dict(message)
                message["content"] = blocks
        return message

    def _mark_last_text_block_cacheable(self, blocks: list[dict[str, Any]]) -> bool:
        for part in reversed(blocks):
            if part.get("type") == "text" and part.get("text"):
                part["cache_control"] = {"type": "ephemeral"}
                return True
        return False

    def _parse_stream_line(
        self,
        line: str,
        in_think: bool,
        tag_buffer: str,
        json_buffer: str = "",
    ) -> tuple[LLMStreamChunk, bool, str, str]:
        """Parse a single SSE line from stream.

        Returns (chunk, new_in_think, new_tag_buffer, new_json_buffer).
        The json_buffer accumulates partial JSON from non-standard APIs that
        split a single JSON object across multiple data: lines.
        """
        chunk = LLMStreamChunk()

        # SSE spec: "data:" may or may not have a space after the colon
        if line.startswith("data: "):
            data_str = line[6:]
        elif line.startswith("data:"):
            data_str = line[5:]
        else:
            # Non-data lines (comments, event types, empty) - never buffer
            return chunk, in_think, tag_buffer, json_buffer

        data_str = data_str.strip()
        if not data_str:
            return chunk, in_think, tag_buffer, json_buffer

        if data_str == "[DONE]":
            chunk.is_finished = True
            return chunk, in_think, tag_buffer, ""

        # Accumulate into json_buffer for split JSON handling
        if json_buffer:
            json_buffer += data_str
        else:
            json_buffer = data_str

        try:
            data = json.loads(json_buffer)
            json_buffer = ""  # Reset on successful parse
        except json.JSONDecodeError:
            # Cap buffer at 64KB to prevent memory leaks
            if len(json_buffer) > 65536:
                logger.warning("[LLM] JSON buffer exceeded 64KB, discarding")
                json_buffer = ""
            return chunk, in_think, tag_buffer, json_buffer

        if "error" in data:
            raise LLMError(f"Stream error: {data['error']}")

        # Parse usage from stream (returned in the final chunk with include_usage)
        if data.get("usage"):
            chunk.usage = data["usage"]

        choices = data.get("choices", [])
        if not choices:
            return chunk, in_think, tag_buffer, json_buffer

        choice = choices[0]
        delta = choice.get("delta", {})

        if choice.get("finish_reason"):
            chunk.finish_reason = choice["finish_reason"]

        # Reasoning content (DeepSeek R1)
        if delta.get("reasoning_content"):
            chunk.reasoning_content = delta["reasoning_content"]

        # Regular content with think tag filtering
        if delta.get("content"):
            text = delta["content"]
            chunk.content, in_think, tag_buffer = self._filter_think_tags(text, in_think, tag_buffer)

        # Tool calls
        if delta.get("tool_calls"):
            for tc_delta in delta["tool_calls"]:
                chunk.tool_call = tc_delta
                break  # Return one at a time

        return chunk, in_think, tag_buffer, json_buffer

    def _filter_think_tags(self, text: str, in_think: bool, tag_buffer: str) -> tuple[str, bool, str]:
        """Filter out <think>...</think> tags from content.

        Returns (filtered_content, new_in_think, new_tag_buffer).
        """
        tag_buffer += text
        emit = ""
        i = 0
        buf = tag_buffer

        while i < len(buf):
            if not in_think:
                # Look for <think open tag
                if buf[i] == "<":
                    tag_candidate = buf[i:]
                    if tag_candidate.startswith("<think>"):
                        in_think = True
                        i += len("<think>")
                        continue
                    if "<think>".startswith(tag_candidate):
                        # Partial match - keep in buffer
                        break
                    emit += buf[i]
                    i += 1
                else:
                    emit += buf[i]
                    i += 1
            else:
                # Inside think - look for </think> close tag
                if buf[i] == "<":
                    tag_candidate = buf[i:]
                    if tag_candidate.startswith("</think>"):
                        in_think = False
                        i += len("</think>")
                        continue
                    if "</think>".startswith(tag_candidate):
                        break
                i += 1

        tag_buffer = buf[i:]
        return emit, in_think, tag_buffer

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
        url = f"{self._normalize_base_url()}/chat/completions"
        payload = self._build_payload(messages, tools, temperature, max_tokens, stream=False, **kwargs)

        client = await self._get_client()
        response = await client.post(url, json=payload, headers=self._get_headers())

        if response.status_code >= 400:
            error_text = response.text[:500]
            raise LLMError(f"HTTP {response.status_code}: {error_text}")

        data = response.json()

        if "error" in data:
            raise LLMError(f"API error: {data['error']}")

        choice = data.get("choices", [{}])[0]
        msg = choice.get("message", {})

        return LLMResponse(
            content=msg.get("content", ""),
            tool_calls=msg.get("tool_calls", []),
            finish_reason=choice.get("finish_reason"),
            usage=data.get("usage"),
            model=data.get("model"),
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
        url = f"{self._normalize_base_url()}/chat/completions"
        payload = self._build_payload(messages, tools, temperature, max_tokens, stream=True, **kwargs)
        full_content = ""
        full_reasoning = ""
        tool_calls_data: list[LLMToolCall] = []
        last_finish_reason: str | None = None
        final_usage: dict[str, int] | None = None

        in_think = False
        tag_buffer = ""
        json_buffer = ""  # Buffer for non-standard APIs with split JSON (inspired by PR #120)

        max_retries = 3
        client = await self._get_client()

        for attempt in range(max_retries):
            try:
                async with client.stream("POST", url, json=payload, headers=self._get_headers()) as resp:
                    if resp.status_code >= 400:
                        error_body = ""
                        async for chunk in resp.aiter_bytes():
                            error_body += chunk.decode(errors="replace")
                        raise LLMError(f"HTTP {resp.status_code}: {error_body[:500]}")

                    async for line in resp.aiter_lines():
                        chunk, in_think, tag_buffer, json_buffer = self._parse_stream_line(
                            line, in_think, tag_buffer, json_buffer
                        )

                        if chunk.is_finished:
                            break

                        if chunk.content:
                            full_content += chunk.content
                            if on_chunk:
                                await on_chunk(chunk.content)

                        if chunk.reasoning_content:
                            full_reasoning += chunk.reasoning_content
                            if on_thinking:
                                await on_thinking(chunk.reasoning_content)

                        if chunk.tool_call:
                            idx = chunk.tool_call.get("index", 0)
                            while len(tool_calls_data) <= idx:
                                tool_calls_data.append({"id": "", "function": {"name": "", "arguments": ""}})
                            tc = tool_calls_data[idx]
                            delta_id = chunk.tool_call.get("id")
                            if delta_id:
                                tc["id"] = delta_id
                            fn_delta = chunk.tool_call.get("function") or {}
                            function = tc.get("function") or {}
                            tool_name = function.get("name") or ""
                            current_arguments = function.get("arguments")
                            arguments = current_arguments if isinstance(current_arguments, str) else ""
                            name_delta = fn_delta.get("name")
                            if name_delta:
                                tool_name += name_delta
                            arg_chunk = fn_delta.get("arguments")
                            if arg_chunk is not None:
                                if isinstance(arg_chunk, dict):
                                    arguments = json.dumps(arg_chunk, ensure_ascii=False)
                                else:
                                    arguments += str(arg_chunk)
                            tc["function"] = {"name": tool_name, "arguments": arguments}
                            if on_tool_delta and (tool_name or arguments):
                                callback_data: ToolCallbackData = {
                                    "id": tc.get("id") or f"draft-{idx}",
                                    "index": idx,
                                    "name": tool_name,
                                    "arguments": arguments,
                                }
                                await on_tool_delta(callback_data)

                        if chunk.usage:
                            final_usage = chunk.usage

                        if chunk.finish_reason:
                            last_finish_reason = chunk.finish_reason

                break  # Success

            except (httpx.ConnectError, httpx.ReadError, httpx.ConnectTimeout) as e:
                if attempt < max_retries - 1:
                    wait = (attempt + 1) * 1
                    logger.warning(f"Stream attempt {attempt + 1} failed ({type(e).__name__}), retrying in {wait}s...")
                    await asyncio.sleep(wait)
                    full_content = ""
                    full_reasoning = ""
                    tool_calls_data = []
                    in_think = False
                    tag_buffer = ""
                    json_buffer = ""
                else:
                    raise LLMError(f"Connection failed after {max_retries} attempts: {e}") from e

        # Clean up any remaining think tags
        full_content = re.sub(r"<think>[\s\S]*?</think>\s*", "", full_content).strip()

        return LLMResponse(
            content=full_content,
            tool_calls=tool_calls_data,
            reasoning_content=full_reasoning or None,
            finish_reason=last_finish_reason,
            usage=final_usage,
            model=self.model,
        )

    @override
    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
