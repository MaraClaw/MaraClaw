"""Gemini native LLM provider client."""

from __future__ import annotations

import json
import re
from typing import Any, ClassVar, override

import httpx

from app.core.json_types import (
    JsonObject,
    is_json_value,
    is_str_dict,
    json_as_int,
    json_as_str,
    json_as_str_or,
    json_loads_object,
    json_loads_value,
    json_object_from,
    json_object_from_response,
    object_list_from_row,
)
from app.services.llm.base import ChunkCallback, LLMClient, LLMError, ThinkingCallback, ToolCallback, ToolDefinition
from app.services.llm.providers.openai_compatible import OpenAICompatibleClient
from app.services.llm.types import LLMMessage, LLMResponse, LLMToolCall, ToolPayload


class GeminiClient(LLMClient):
    """Client for Gemini native API (`generateContent` / `streamGenerateContent`)."""

    DEFAULT_BASE_URL: ClassVar[str] = "https://generativelanguage.googleapis.com/v1beta"

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
        self._openai_fallback_client: OpenAICompatibleClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout, follow_redirects=True, proxy=None)
        return self._client

    async def _get_openai_fallback_client(self) -> OpenAICompatibleClient:
        """Fallback for legacy `/openai` base URL deployments."""
        if self._openai_fallback_client is None:
            self._openai_fallback_client = OpenAICompatibleClient(
                api_key=self.api_key,
                base_url=self.base_url,
                model=self.model,
                timeout=self.timeout,
                supports_tool_choice=self.supports_tool_choice,
                supports_cache_control=False,
            )
        return self._openai_fallback_client

    def _is_openai_compatible_base(self) -> bool:
        """Detect legacy OpenAI-compatible Gemini gateway endpoint."""
        url = (self.base_url or self.DEFAULT_BASE_URL).rstrip("/").lower()
        return url.endswith("/openai") or "/openai/" in url

    @override
    def _get_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-goog-api-key": self.api_key,
        }

    def _normalize_base_url(self) -> str:
        """Normalize base URL for Gemini native endpoints."""
        url = (self.base_url or self.DEFAULT_BASE_URL).rstrip("/")
        if "/models/" in url and url.endswith((":generateContent", ":streamGenerateContent")):
            url = url.split("/models/")[0]
        return url

    def _normalize_model_name(self) -> str:
        """Normalize model id for native Gemini endpoint path."""
        model = (self.model or "").strip()
        if model.startswith("models/"):
            model = model[len("models/") :]
        return model

    def _parse_data_url_image(self, data_url: str) -> tuple[str, str] | None:
        """Parse data URL into (mime_type, base64_data)."""
        m = re.match(r"^data:([^;]+);base64,([A-Za-z0-9+/=]+)$", data_url or "")
        if not m:
            return None
        return m.group(1), m.group(2)

    def _content_to_gemini_parts(self, content: object) -> list[JsonObject]:
        """Convert canonical content into Gemini `parts`."""
        if content is None:
            return []

        if isinstance(content, str):
            return [{"text": content}]

        if isinstance(content, list):
            parts: list[JsonObject] = []
            content_parts: list[object] = list(content)
            for raw_part in content_parts:
                if not is_str_dict(raw_part):
                    continue
                part = json_object_from(raw_part)
                ptype = json_as_str(part.get("type"))
                if ptype == "text":
                    text = json_as_str_or(part.get("text"))
                    if text:
                        parts.append({"text": text})
                elif ptype == "image_url":
                    image_obj = json_object_from(part.get("image_url"))
                    image_url = json_as_str_or(image_obj.get("url"))
                    parsed = self._parse_data_url_image(image_url)
                    if parsed:
                        mime_type, b64_data = parsed
                        parts.append(
                            {
                                "inlineData": {
                                    "mimeType": mime_type,
                                    "data": b64_data,
                                }
                            }
                        )
                    elif image_url:
                        # Gemini native API requires uploaded files or inline data;
                        # preserve reference in text when URL cannot be inlined.
                        parts.append({"text": f"[image_url:{image_url}]"})
            return parts

        return [{"text": str(content)}]

    def _extract_tool_name_map(self, messages: list[LLMMessage]) -> dict[str, str]:
        """Build tool_call_id -> function_name map from assistant messages."""
        out: dict[str, str] = {}
        for msg in messages:
            if msg.role != "assistant" or not msg.tool_calls:
                continue
            for tc in msg.tool_calls:
                tc_id = tc.get("id")
                tc_name = tc.get("function", {}).get("name")
                if tc_id and tc_name:
                    out[tc_id] = tc_name
        return out

    def _convert_tools(
        self, tools: list[ToolDefinition] | None
    ) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
        """Convert OpenAI-style tools to Gemini function declarations."""
        if not tools:
            return None, None

        declarations: list[dict[str, Any]] = []
        for tool in tools:
            if tool.get("type") != "function":
                continue
            function = tool.get("function")
            if function is None:
                continue
            decl: dict[str, Any] = {
                "name": function.get("name", ""),
                "description": function.get("description", ""),
            }
            params = function.get("parameters")
            if isinstance(params, dict):
                decl["parameters"] = params
            declarations.append(decl)

        if not declarations:
            return None, None

        tools_payload = [{"functionDeclarations": declarations}]
        tool_config = None
        if self.supports_tool_choice:
            tool_config = {"functionCallingConfig": {"mode": "AUTO"}}
        return tools_payload, tool_config

    def _build_payload(
        self,
        messages: list[LLMMessage],
        tools: list[ToolDefinition] | None,
        temperature: float | None,
        max_tokens: int | None,
        **kwargs: object,
    ) -> dict[str, Any]:
        """Build Gemini request payload."""
        system_blocks: list[str] = []
        contents: list[dict[str, Any]] = []
        tool_name_map = self._extract_tool_name_map(messages)

        for msg in messages:
            if msg.role == "system":
                parts = self._content_to_gemini_parts(msg.content)
                text_chunks = [json_as_str_or(p.get("text")) for p in parts if p.get("text")]
                if text_chunks:
                    system_blocks.append("\n".join(text_chunks))
                continue

            if msg.role == "user":
                parts = self._content_to_gemini_parts(msg.content)
                if parts:
                    contents.append({"role": "user", "parts": parts})
                continue

            if msg.role == "assistant":
                parts = self._content_to_gemini_parts(msg.content)
                if msg.tool_calls:
                    for tc in msg.tool_calls:
                        fn = tc.get("function", {})
                        args = fn.get("arguments", "{}")
                        if isinstance(args, str):
                            try:
                                parsed_args = json_loads_object(args)
                            except json.JSONDecodeError:
                                parsed_args = {}
                        elif is_str_dict(args):
                            parsed_args = json_object_from(args)
                        else:
                            parsed_args = {}

                        func_call_dict: JsonObject = {
                            "name": json_as_str_or(fn.get("name")),
                            "args": parsed_args,
                        }
                        extra = tc.get("_gemini_extra")
                        if extra:
                            func_call_dict.update(json_object_from(extra))

                        parts.append({"functionCall": func_call_dict})
                if parts:
                    contents.append({"role": "model", "parts": parts})
                continue

            if msg.role == "tool":
                name = tool_name_map.get(msg.tool_call_id or "", msg.tool_call_id or "tool_result")
                response_content = msg.content or ""
                if isinstance(response_content, str):
                    try:
                        parsed_raw = json_loads_value(response_content)
                        if is_str_dict(parsed_raw):
                            response_obj: JsonObject = json_object_from(parsed_raw)
                        elif is_json_value(parsed_raw):
                            response_obj = {"result": parsed_raw}
                        else:
                            response_obj = {"result": str(parsed_raw)}
                    except json.JSONDecodeError:
                        response_obj = {"result": response_content}
                elif is_str_dict(response_content):
                    response_obj = json_object_from(response_content)
                else:
                    response_obj = {"result": str(response_content)}

                contents.append(
                    {
                        "role": "user",
                        "parts": [
                            {
                                "functionResponse": {
                                    "name": name,
                                    "response": response_obj,
                                }
                            }
                        ],
                    }
                )

        payload: dict[str, Any] = {
            "contents": contents or [{"role": "user", "parts": [{"text": ""}]}],
            "generationConfig": {
                "temperature": temperature,
            },
        }

        if max_tokens:
            payload["generationConfig"]["maxOutputTokens"] = max_tokens

        if system_blocks:
            payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_blocks)}]}

        tools_payload, tool_config = self._convert_tools(tools)
        if tools_payload:
            payload["tools"] = tools_payload
        if tool_config:
            payload["toolConfig"] = tool_config

        from app.services.llm.reasoning import apply_reasoning_effort

        apply_reasoning_effort(
            payload,
            provider=str(kwargs.pop("llm_provider", "") or "gemini"),
            effort=kwargs.pop("reasoning_effort", None),
        )
        payload.update(kwargs)
        return payload

    def _normalize_usage(self, usage: object) -> dict[str, int] | None:
        """Normalize Gemini usage metadata to unified usage dict."""
        if not isinstance(usage, dict):
            return None
        usage_obj = json_object_from(usage)
        input_tokens = json_as_int(usage_obj.get("promptTokenCount"))
        output_tokens = json_as_int(usage_obj.get("candidatesTokenCount"))
        total_raw: object = usage_obj.get("totalTokenCount")
        total_tokens = json_as_int(total_raw, input_tokens + output_tokens)
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        }

    def _normalize_finish_reason(self, finish_reason: str | None, tool_calls: list[LLMToolCall]) -> str | None:
        """Normalize Gemini finish reason to OpenAI-style labels."""
        if tool_calls:
            return "tool_calls"
        if not finish_reason:
            return None
        mapping = {
            "STOP": "stop",
            "MAX_TOKENS": "length",
            "SAFETY": "content_filter",
            "RECITATION": "content_filter",
        }
        return mapping.get(finish_reason, "stop")

    def _parse_response_data(self, data: JsonObject) -> LLMResponse:
        """Convert Gemini native response into canonical LLMResponse."""
        content_chunks: list[str] = []
        tool_calls: list[LLMToolCall] = []
        seen_tool_calls: set[str] = set()
        finish_reason = None

        candidates = object_list_from_row(data.get("candidates"))
        if candidates:
            candidate = json_object_from(candidates[0])
            finish_reason = json_as_str(candidate.get("finishReason"))
            content_obj = json_object_from(candidate.get("content"))
            for part_raw in object_list_from_row(content_obj.get("parts")):
                part = json_object_from(part_raw)
                text = json_as_str(part.get("text"))
                if text:
                    content_chunks.append(text)
                function_call = json_object_from(part.get("functionCall")) if part.get("functionCall") else {}
                if function_call:
                    name = json_as_str_or(function_call.get("name"))
                    args_raw: object = function_call.get("args")
                    args_str = json.dumps(json_object_from(args_raw), ensure_ascii=False)
                    dedup_key = f"{name}:{args_str}"
                    if dedup_key in seen_tool_calls:
                        continue
                    seen_tool_calls.add(dedup_key)

                    extra: ToolPayload = {
                        key: value for key, value in function_call.items() if key not in ["name", "args"]
                    }

                    tool_calls.append(
                        {
                            "id": f"call_{len(tool_calls) + 1}",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": args_str,
                            },
                            "_gemini_extra": extra,
                        }
                    )

        usage = self._normalize_usage(data.get("usageMetadata"))

        return LLMResponse(
            content="".join(content_chunks),
            tool_calls=tool_calls,
            finish_reason=self._normalize_finish_reason(finish_reason, tool_calls),
            usage=usage,
            model=json_as_str(data.get("modelVersion")) or self.model,
        )

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
        if self._is_openai_compatible_base():
            fallback = await self._get_openai_fallback_client()
            return await fallback.complete(
                messages=messages,
                tools=tools,
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs,
            )

        model_name = self._normalize_model_name()
        url = f"{self._normalize_base_url()}/models/{model_name}:generateContent"
        payload = self._build_payload(messages, tools, temperature, max_tokens, **kwargs)

        client = await self._get_client()
        response = await client.post(url, json=payload, headers=self._get_headers())

        if response.status_code >= 400:
            error_text = response.text[:500]
            raise LLMError(f"HTTP {response.status_code}: {error_text}")

        data = json_object_from_response(response)
        if data.get("error"):
            raise LLMError(f"API error: {data['error']}")

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
        """Streaming completion using Gemini SSE endpoint."""
        if self._is_openai_compatible_base():
            fallback = await self._get_openai_fallback_client()
            return await fallback.stream(
                messages=messages,
                tools=tools,
                temperature=temperature,
                max_tokens=max_tokens,
                on_chunk=on_chunk,
                on_tool_delta=on_tool_delta,
                on_thinking=on_thinking,
                **kwargs,
            )

        model_name = self._normalize_model_name()
        url = f"{self._normalize_base_url()}/models/{model_name}:streamGenerateContent"
        payload = self._build_payload(messages, tools, temperature, max_tokens, **kwargs)

        full_text = ""
        tool_calls: list[LLMToolCall] = []
        seen_tool_calls: set[str] = set()
        final_usage: dict[str, int] | None = None
        final_finish_reason: str | None = None

        client = await self._get_client()

        try:
            async with client.stream(
                "POST",
                url,
                params={"alt": "sse"},
                json=payload,
                headers=self._get_headers(),
            ) as resp:
                if resp.status_code >= 400:
                    error_body = ""
                    async for chunk in resp.aiter_bytes():
                        error_body += chunk.decode(errors="replace")
                    raise LLMError(f"HTTP {resp.status_code}: {error_body[:500]}")

                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data_str = line[len("data:") :].strip()
                    if not data_str or data_str == "[DONE]":
                        continue

                    try:
                        parsed = json_loads_value(data_str)
                    except json.JSONDecodeError:
                        continue

                    if not is_str_dict(parsed):
                        continue
                    data = json_object_from(parsed)
                    if data.get("error"):
                        raise LLMError(f"API error: {data['error']}")

                    usage = self._normalize_usage(data.get("usageMetadata"))
                    if usage:
                        final_usage = usage

                    candidates = object_list_from_row(data.get("candidates"))
                    if not candidates:
                        continue
                    candidate = json_object_from(candidates[0])
                    final_finish_reason = json_as_str(candidate.get("finishReason")) or final_finish_reason
                    content_obj = json_object_from(candidate.get("content"))
                    for part_raw in object_list_from_row(content_obj.get("parts")):
                        part = json_object_from(part_raw)
                        text = json_as_str(part.get("text"))
                        if text:
                            full_text += text
                            if on_chunk:
                                await on_chunk(text)

                        function_call = json_object_from(part.get("functionCall")) if part.get("functionCall") else {}
                        if function_call:
                            name = json_as_str_or(function_call.get("name"))
                            args_raw: object = function_call.get("args")
                            args_str = json.dumps(json_object_from(args_raw), ensure_ascii=False)
                            dedup_key = f"{name}:{args_str}"
                            if dedup_key in seen_tool_calls:
                                continue
                            seen_tool_calls.add(dedup_key)

                            extra: ToolPayload = {
                                key: value for key, value in function_call.items() if key not in ["name", "args"]
                            }

                            tool_calls.append(
                                {
                                    "id": f"call_{len(tool_calls) + 1}",
                                    "type": "function",
                                    "function": {
                                        "name": name,
                                        "arguments": args_str,
                                    },
                                    "_gemini_extra": extra,
                                }
                            )

        except (httpx.ConnectError, httpx.ReadError, httpx.ConnectTimeout) as e:
            raise LLMError(f"Connection failed: {e}") from e

        return LLMResponse(
            content=full_text,
            tool_calls=tool_calls,
            finish_reason=self._normalize_finish_reason(final_finish_reason, tool_calls),
            usage=final_usage,
            model=self.model,
        )

    @override
    async def close(self) -> None:
        """Close the HTTP client."""
        if self._openai_fallback_client:
            await self._openai_fallback_client.close()
        if self._client and not self._client.is_closed:
            await self._client.aclose()
