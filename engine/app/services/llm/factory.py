"""LLM client factory and high-level chat helpers."""

from __future__ import annotations

from typing import Literal, TypedDict

from app.services.llm.base import ChunkCallback, LLMClient, ThinkingCallback, ToolDefinition
from app.services.llm.providers import AnthropicClient, GeminiClient, OpenAICompatibleClient, OpenAIResponsesClient
from app.services.llm.registry import (
    PROVIDER_CLIENTS,
    PROVIDER_URLS,
    TOOL_CHOICE_PROVIDERS,
    get_max_tokens,
    get_provider_base_url,
    get_provider_spec,
    normalize_provider,
)
from app.services.llm.types import LLMMessage, LLMToolCall, LLMUsage, OpenAIMessage


class ChatChoiceMessage(TypedDict):
    """Assistant message returned by the chat helper surface."""

    role: Literal["assistant"]
    content: str
    tool_calls: list[LLMToolCall] | None


class ChatChoice(TypedDict):
    """OpenAI-style completed chat choice."""

    message: ChatChoiceMessage
    finish_reason: str


class ChatCompletion(TypedDict):
    """OpenAI-style response returned by the chat helper surface."""

    choices: list[ChatChoice]
    model: str
    usage: LLMUsage


def create_llm_client(
    provider: str,
    api_key: str,
    model: str,
    base_url: str | None = None,
    timeout: float = 120.0,
) -> LLMClient:
    """Create an LLM client for the given provider."""
    normalized_provider = normalize_provider(provider)
    spec = get_provider_spec(normalized_provider)
    final_base_url = get_provider_base_url(normalized_provider, base_url)

    if spec and spec.protocol == "anthropic":
        return AnthropicClient(
            api_key=api_key,
            base_url=final_base_url,
            model=model,
            timeout=timeout,
        )

    if spec and spec.protocol == "openai_responses":
        return OpenAIResponsesClient(
            api_key=api_key,
            base_url=final_base_url,
            model=model,
            timeout=timeout,
            supports_tool_choice=spec.supports_tool_choice,
        )

    if spec and spec.protocol == "gemini":
        return GeminiClient(
            api_key=api_key,
            base_url=final_base_url,
            model=model,
            timeout=timeout,
            supports_tool_choice=spec.supports_tool_choice,
        )

    if normalized_provider in PROVIDER_CLIENTS:
        supports_tool_choice = normalized_provider in TOOL_CHOICE_PROVIDERS
        return OpenAICompatibleClient(
            api_key=api_key,
            base_url=final_base_url,
            model=model,
            timeout=timeout,
            supports_tool_choice=supports_tool_choice,
            supports_cache_control=normalized_provider == "qwen",
        )

    return OpenAICompatibleClient(
        api_key=api_key,
        base_url=final_base_url or PROVIDER_URLS["openai"],
        model=model,
        timeout=timeout,
        supports_tool_choice=True,
        supports_cache_control=False,
    )


async def chat_complete(
    provider: str,
    api_key: str,
    model: str,
    messages: list[OpenAIMessage],
    base_url: str | None = None,
    tools: list[ToolDefinition] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: float = 120.0,  # noqa: ASYNC109
) -> ChatCompletion:
    """High-level function for non-streaming chat completion."""
    client = create_llm_client(provider, api_key, model, base_url, timeout)

    try:
        llm_messages = [
            LLMMessage(
                role=message["role"],
                content=message.get("content"),
                tool_calls=message.get("tool_calls"),
                tool_call_id=message.get("tool_call_id"),
                reasoning_content=message.get("reasoning_content"),
            )
            for message in messages
        ]
        response = await client.complete(
            messages=llm_messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens or get_max_tokens(provider, model),
        )

        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": response.content,
                        "tool_calls": response.tool_calls or None,
                    },
                    "finish_reason": response.finish_reason or "stop",
                }
            ],
            "model": response.model or model,
            "usage": response.usage or {},
        }
    finally:
        await client.close()


async def chat_stream(
    provider: str,
    api_key: str,
    model: str,
    messages: list[OpenAIMessage],
    base_url: str | None = None,
    tools: list[ToolDefinition] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: float = 120.0,  # noqa: ASYNC109
    on_chunk: ChunkCallback | None = None,
    on_thinking: ThinkingCallback | None = None,
) -> ChatCompletion:
    """High-level function for streaming chat completion."""
    client = create_llm_client(provider, api_key, model, base_url, timeout)

    try:
        llm_messages = [
            LLMMessage(
                role=message["role"],
                content=message.get("content"),
                tool_calls=message.get("tool_calls"),
                tool_call_id=message.get("tool_call_id"),
                reasoning_content=message.get("reasoning_content"),
            )
            for message in messages
        ]
        response = await client.stream(
            messages=llm_messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens or get_max_tokens(provider, model),
            on_chunk=on_chunk,
            on_thinking=on_thinking,
        )

        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": response.content,
                        "tool_calls": response.tool_calls or None,
                    },
                    "finish_reason": response.finish_reason or "stop",
                }
            ],
            "model": response.model or model,
            "usage": response.usage or {},
        }
    finally:
        await client.close()


__all__ = [
    "chat_complete",
    "chat_stream",
    "create_llm_client",
]
