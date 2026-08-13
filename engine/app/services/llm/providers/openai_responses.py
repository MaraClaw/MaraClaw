"""OpenAI Responses API LLM provider client."""

from __future__ import annotations

import json
from typing import Any, override

import httpx

from app.core.logging import logger
from app.services.llm.base import ChunkCallback, LLMClient, LLMError, ThinkingCallback, ToolCallback, ToolDefinition
from app.services.llm.types import LLMMessage, LLMResponse, LLMToolCall


class OpenAIResponsesClient(LLMClient):
    """Client for OpenAI Responses API (`/v1/responses`)."""

    base_url: str
    DEFAULT_BASE_URL = "https://api.openai.com/v1"

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 120.0,
        supports_tool_choice: bool = True,
    ):
        super().__init__(api_key, base_url or self.DEFAULT_BASE_URL, model, timeout)
        self.supports_tool_choice = supports_tool_choice
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
        url = self.base_url.rstrip("/")
        if url.endswith("/responses"):
            url = url[: -len("/responses")]
        return url

    def _format_content_for_input(self, content: Any) -> Any:
        """Convert OpenAI chat-style content into Responses API input content."""
        if not isinstance(content, list):
            return content

        formatted: list[dict[str, Any]] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "text":
                formatted.append({"type": "input_text", "text": part.get("text", "")})
            elif ptype == "image_url":
                img = part.get("image_url", {})
                if isinstance(img, dict):
                    formatted.append({"type": "input_image", "image_url": img.get("url", "")})
            else:
                formatted.append(part)
        return formatted if formatted else content

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
            if item.get("type") == "function_call":
                call_id = item.get("call_id", "")
                if call_id:
                    call_ids_with_fc.add(call_id)

        # Collect all call_ids from function_call_output items
        call_ids_with_fco: set[str] = set()
        for item in items:
            if item.get("type") == "function_call_output":
                call_id = item.get("call_id", "")
                if call_id:
                    call_ids_with_fco.add(call_id)

        # Determine which call_ids are orphaned (output without call, or call without output)
        orphaned_fco = call_ids_with_fco - call_ids_with_fc
        orphaned_fc = call_ids_with_fc - call_ids_with_fco

        if not orphaned_fco and not orphaned_fc:
            return items

        if orphaned_fco:
            logger.warning(
                "[OpenAIResponses] Removing %d orphaned function_call_output item(s) "
                "with no matching function_call: %s",
                len(orphaned_fco),
                orphaned_fco,
            )
        if orphaned_fc:
            logger.warning(
                "[OpenAIResponses] Removing %d orphaned function_call item(s) "
                "with no matching function_call_output: %s",
                len(orphaned_fc),
                orphaned_fc,
            )

        # Filter out orphaned items
        return [
            item
            for item in items
            if not (
                (item.get("type") == "function_call_output" and item.get("call_id", "") in orphaned_fco)
                or (item.get("type") == "function_call" and item.get("call_id", "") in orphaned_fc)
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
            fn = function if isinstance(function, dict) else {}
            converted.append(
                {
                    "type": "function",
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters", {"type": "object"}),
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
        **kwargs: Any,
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

    def _parse_response_data(self, data: dict[str, Any]) -> LLMResponse:
        """Convert Responses API payload into canonical LLMResponse."""
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list[LLMToolCall] = []

        for item in data.get("output", []) or []:
            item_type = item.get("type")
            if item_type == "message":
                for c in item.get("content", []) or []:
                    c_type = c.get("type")
                    if c_type in {"output_text", "text"}:
                        content_parts.append(c.get("text", ""))
                    elif c_type == "reasoning":
                        reasoning_parts.append(c.get("summary", "") or c.get("text", ""))
            elif item_type == "function_call":
                args = item.get("arguments", "{}")
                if isinstance(args, dict):
                    args = json.dumps(args, ensure_ascii=False)
                tool_calls.append(
                    {
                        "id": item.get("call_id") or item.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": item.get("name", ""),
                            "arguments": str(args or "{}"),
                        },
                    }
                )

        # Some Responses payloads include a pre-aggregated output_text field.
        # Use it as a fallback when output blocks are empty.
        if not content_parts and data.get("output_text"):
            content_parts.append(str(data.get("output_text", "")))

        usage = data.get("usage")
        finish_reason = "tool_calls" if tool_calls else "stop"

        return LLMResponse(
            content="".join(content_parts),
            tool_calls=tool_calls,
            reasoning_content="".join(reasoning_parts) or None,
            finish_reason=finish_reason,
            usage=usage if isinstance(usage, dict) else None,
            model=data.get("model"),
        )

    def _extract_api_error(self, data: dict[str, Any]) -> str | None:
        """Extract meaningful error message from Responses API payload."""
        # OpenAI Responses often returns `"error": null` on success,
        # so we must only treat it as error when it's truthy.
        err = data.get("error")
        if err:
            if isinstance(err, dict):
                msg = err.get("message") or str(err)
                err_type = err.get("type")
                err_code = err.get("code")
                extra = []
                if err_type:
                    extra.append(f"type={err_type}")
                if err_code:
                    extra.append(f"code={err_code}")
                suffix = f" ({', '.join(extra)})" if extra else ""
                return f"{msg}{suffix}"
            return str(err)

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

    def _build_error_log_context(self, data: dict[str, Any]) -> dict[str, Any]:
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
        **kwargs: Any,
    ) -> LLMResponse:
        """Non-streaming completion."""
        url = f"{self._normalize_base_url()}/responses"
        payload = self._build_payload(messages, tools, temperature, max_tokens, stream=False, **kwargs)

        client = await self._get_client()
        response = await client.post(url, json=payload, headers=self._get_headers())

        if response.status_code >= 400:
            error_text = response.text[:500]
            raise LLMError(f"HTTP {response.status_code}: {error_text}")

        data = response.json()
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
        **kwargs: Any,
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
