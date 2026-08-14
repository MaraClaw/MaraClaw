"""OpenAI Responses API LLM provider client."""

from __future__ import annotations

import json
from typing import Any, ClassVar, override

import httpx

from app.core.json_types import (
    JsonObject,
    is_str_dict,
    json_as_str,
    json_as_str_or,
    json_object_from,
    json_object_from_response,
    object_list_from_row,
)
from app.core.logging import logger
from app.services.llm.base import ChunkCallback, LLMClient, LLMError, ThinkingCallback, ToolCallback, ToolDefinition
from app.services.llm.types import LLMMessage, LLMResponse, LLMToolCall, LLMUsage


def _mapping_str(mapping: dict[str, object], key: str, default: str = "") -> str:
    return json_as_str_or(mapping.get(key), default)


def _usage_from_json(value: object) -> LLMUsage | None:
    usage_obj = json_object_from(value)
    if not usage_obj:
        return None
    usage: LLMUsage = {}
    for key, raw in usage_obj.items():
        if isinstance(raw, int) and not isinstance(raw, bool):
            usage[key] = raw
    return usage or None


class OpenAIResponsesClient(LLMClient):
    """Client for OpenAI Responses API (`/v1/responses`)."""

    DEFAULT_BASE_URL: ClassVar[str] = "https://api.openai.com/v1"

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 120.0,
        supports_tool_choice: bool = True,
    ):
        super().__init__(api_key, base_url or self.DEFAULT_BASE_URL, model, timeout)
        self.supports_tool_choice: bool = supports_tool_choice
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
        """Normalize base URL by stripping trailing /responses endpoint."""
        url = (self.base_url or self.DEFAULT_BASE_URL).rstrip("/")
        if url.endswith("/responses"):
            url = url[: -len("/responses")]
        return url

    def _format_content_for_input(self, content: object) -> object:
        """Convert OpenAI chat-style content into Responses API input content."""
        if not isinstance(content, list):
            return content

        content_parts: list[object] = list(content)
        formatted: list[dict[str, Any]] = []
        for raw_part in content_parts:
            if not is_str_dict(raw_part):
                continue
            part = json_object_from(raw_part)
            ptype = json_as_str(part.get("type"))
            if ptype == "text":
                formatted.append({"type": "input_text", "text": json_as_str_or(part.get("text"))})
            elif ptype == "image_url":
                img = json_object_from(part.get("image_url"))
                formatted.append({"type": "input_image", "image_url": json_as_str_or(img.get("url"))})
            else:
                formatted.append(part)
        return formatted if formatted else content_parts

    def _messages_to_input(self, messages: list[LLMMessage]) -> list[dict[str, Any]]:
        """Convert canonical message format to Responses API input format."""
        input_items: list[dict[str, Any]] = []

        for msg in messages:
            # Handle system messages with dynamic_content
            if msg.role == "system" and msg.content is not None:
                content = msg.content
                if msg.dynamic_content:
                    content = f"{content}\n\n{msg.dynamic_content}"
                input_items.append(
                    {
                        "role": msg.role,
                        "content": self._format_content_for_input(content),
                    }
                )
            elif msg.role in {"user", "assistant"} and msg.content is not None:
                input_items.append(
                    {
                        "role": msg.role,
                        "content": self._format_content_for_input(msg.content),
                    }
                )

            if msg.role == "assistant" and msg.tool_calls:
                for tc in msg.tool_calls:
                    fn = tc.get("function", {})
                    args = fn.get("arguments", "{}")
                    if isinstance(args, dict):
                        args = json.dumps(args, ensure_ascii=False)
                    input_items.append(
                        {
                            "type": "function_call",
                            "call_id": tc.get("id", ""),
                            "name": fn.get("name", ""),
                            "arguments": str(args or "{}"),
                        }
                    )

            if msg.role == "tool":
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": msg.tool_call_id or "",
                        "output": msg.content or "",
                    }
                )

        # Sanitize: ensure every function_call_output has a matching function_call.
        # This prevents "No tool call found for function call output" API errors
        # caused by context window truncation breaking assistant+tool pairs.
        return self._sanitize_input_items(input_items)

    @staticmethod
    def _sanitize_input_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Remove orphaned function_call_output items that have no matching function_call.

        Also removes function_call items whose function_call_output is missing,
        since the Responses API requires complete pairs.
        """
        # Collect all call_ids from function_call items
        call_ids_with_fc: set[str] = set()
        for item in items:
            call_id = _mapping_str(item, "call_id")
            if _mapping_str(item, "type") == "function_call" and call_id:
                call_ids_with_fc.add(call_id)

        # Collect all call_ids from function_call_output items
        call_ids_with_fco: set[str] = set()
        for item in items:
            call_id = _mapping_str(item, "call_id")
            if _mapping_str(item, "type") == "function_call_output" and call_id:
                call_ids_with_fco.add(call_id)

        # Determine which call_ids are orphaned (output without call, or call without output)
        orphaned_fco = call_ids_with_fco - call_ids_with_fc
        orphaned_fc = call_ids_with_fc - call_ids_with_fco

        if not orphaned_fco and not orphaned_fc:
            return items

        if orphaned_fco:
            logger.warning(
                "[OpenAIResponses] Removing %d orphaned function_call_output item(s) "
                + "with no matching function_call: %s",
                len(orphaned_fco),
                orphaned_fco,
            )
        if orphaned_fc:
            logger.warning(
                "[OpenAIResponses] Removing %d orphaned function_call item(s) "
                + "with no matching function_call_output: %s",
                len(orphaned_fc),
                orphaned_fc,
            )

        # Filter out orphaned items
        return [
            item
            for item in items
            if not (
                (_mapping_str(item, "type") == "function_call_output" and _mapping_str(item, "call_id") in orphaned_fco)
                or (_mapping_str(item, "type") == "function_call" and _mapping_str(item, "call_id") in orphaned_fc)
            )
        ]

    def _convert_tools(self, tools: list[ToolDefinition] | None) -> list[dict[str, Any]] | None:
        """Convert OpenAI tool schema to Responses API function tool schema."""
        if not tools:
            return None

        converted: list[dict[str, Any]] = []
        for tool in tools:
            if tool.get("type") != "function":
                continue
            function = tool.get("function")
            if function is None:
                continue
            converted.append(
                {
                    "type": "function",
                    "name": function.get("name", ""),
                    "description": function.get("description", ""),
                    "parameters": function.get("parameters", {"type": "object"}),
                }
            )
        return converted or None

    def _build_payload(
        self,
        messages: list[LLMMessage],
        tools: list[ToolDefinition] | None,
        temperature: float | None,
        max_tokens: int | None,
        stream: bool = False,
        **kwargs: object,
    ) -> dict[str, Any]:
        """Build request payload."""
        payload: dict[str, Any] = {
            "model": self.model,
            "input": self._messages_to_input(messages),
            "temperature": temperature,
            "stream": stream,
        }

        if max_tokens:
            payload["max_output_tokens"] = max_tokens

        converted_tools = self._convert_tools(tools)
        if converted_tools:
            payload["tools"] = converted_tools
            if self.supports_tool_choice:
                payload["tool_choice"] = "auto"

        payload.update(kwargs)
        return payload

    def _parse_response_data(self, data: JsonObject) -> LLMResponse:
        """Convert Responses API payload into canonical LLMResponse."""
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list[LLMToolCall] = []

        for item_raw in object_list_from_row(data.get("output")):
            item = json_object_from(item_raw)
            item_type = json_as_str(item.get("type"))
            if item_type == "message":
                for content_raw in object_list_from_row(item.get("content")):
                    content_item = json_object_from(content_raw)
                    c_type = json_as_str(content_item.get("type"))
                    if c_type in {"output_text", "text"}:
                        content_parts.append(json_as_str_or(content_item.get("text")))
                    elif c_type == "reasoning":
                        reasoning_parts.append(
                            json_as_str_or(content_item.get("summary")) or json_as_str_or(content_item.get("text"))
                        )
            elif item_type == "function_call":
                args_raw: object = item.get("arguments", "{}")
                if is_str_dict(args_raw):
                    args = json.dumps(json_object_from(args_raw), ensure_ascii=False)
                else:
                    args = json_as_str_or(args_raw, "{}")
                tool_calls.append(
                    {
                        "id": json_as_str(item.get("call_id")) or json_as_str_or(item.get("id")),
                        "type": "function",
                        "function": {
                            "name": json_as_str_or(item.get("name")),
                            "arguments": args or "{}",
                        },
                    }
                )

        # Some Responses payloads include a pre-aggregated output_text field.
        # Use it as a fallback when output blocks are empty.
        output_text = json_as_str(data.get("output_text"))
        if not content_parts and output_text:
            content_parts.append(output_text)

        finish_reason = "tool_calls" if tool_calls else "stop"

        return LLMResponse(
            content="".join(content_parts),
            tool_calls=tool_calls,
            reasoning_content="".join(reasoning_parts) or None,
            finish_reason=finish_reason,
            usage=_usage_from_json(data.get("usage")),
            model=json_as_str(data.get("model")),
        )

    def _extract_api_error(self, data: JsonObject) -> str | None:
        """Extract meaningful error message from Responses API payload."""
        # OpenAI Responses often returns `"error": null` on success,
        # so we must only treat it as error when it's truthy.
        err_raw: object = data.get("error")
        if err_raw:
            if is_str_dict(err_raw):
                err = json_object_from(err_raw)
                msg = json_as_str(err.get("message")) or str(err_raw)
                err_type = json_as_str(err.get("type"))
                err_code = json_as_str(err.get("code"))
                extra = []
                if err_type:
                    extra.append(f"type={err_type}")
                if err_code:
                    extra.append(f"code={err_code}")
                suffix = f" ({', '.join(extra)})" if extra else ""
                return f"{msg}{suffix}"
            return str(err_raw)

        status = str(data.get("status") or "").lower()
        if status in {"failed", "incomplete", "cancelled"}:
            last_error = data.get("last_error")
            incomplete = data.get("incomplete_details")
            rid = data.get("id")
            details: list[str] = [f"status={status}"]
            if rid:
                details.append(f"id={rid}")
            if last_error:
                details.append(f"last_error={last_error}")
            if incomplete:
                details.append(f"incomplete_details={incomplete}")
            return "Responses API returned non-success status: " + "; ".join(details)

        return None

    def _build_error_log_context(self, data: JsonObject) -> dict[str, Any]:
        """Build compact context for error logs."""
        return {
            "provider": "openai-response",
            "model": self.model,
            "response_id": data.get("id"),
            "status": data.get("status"),
            "incomplete_details": data.get("incomplete_details"),
            "last_error": data.get("last_error"),
            "has_output": bool(data.get("output")),
        }

    @override
    async def complete(
        self,
        messages: list[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: object,
    ) -> LLMResponse:
        """Non-streaming completion."""
        url = f"{self._normalize_base_url()}/responses"
        payload = self._build_payload(messages, tools, temperature, max_tokens, stream=False, **kwargs)

        client = await self._get_client()
        response = await client.post(url, json=payload, headers=self._get_headers())

        if response.status_code >= 400:
            error_text = response.text[:500]
            raise LLMError(f"HTTP {response.status_code}: {error_text}")

        data = json_object_from_response(response)
        api_error = self._extract_api_error(data)
        if api_error:
            ctx = self._build_error_log_context(data)
            logger.error(
                "OpenAIResponses API error: %s | context=%s",
                api_error,
                ctx,
            )
            raise LLMError(api_error)

        return self._parse_response_data(data)

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
        **kwargs: object,
    ) -> LLMResponse:
        """Streaming completion.

        Minimal implementation: fallback to non-streaming and forward final text.
        """
        response = await self.complete(
            messages=messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )
        if on_chunk and response.content:
            await on_chunk(response.content)
        if on_thinking and response.reasoning_content:
            await on_thinking(response.reasoning_content)
        return response

    @override
    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
