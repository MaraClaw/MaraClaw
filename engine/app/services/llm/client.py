"""Compatibility facade for legacy LLM client imports."""

from __future__ import annotations

from app.services.llm.base import (
    ChunkCallback,
    LLMClient,
    LLMError as LLMError,
    ThinkingCallback,
    ToolCallback as ToolCallback,
)
from app.services.llm.factory import chat_complete, chat_stream, create_llm_client
from app.services.llm.providers import AnthropicClient, GeminiClient, OpenAICompatibleClient, OpenAIResponsesClient
from app.services.llm.registry import (
    MAX_TOKENS_BY_MODEL,
    MAX_TOKENS_BY_PROVIDER,
    PROVIDER_ALIASES,
    PROVIDER_CLIENTS,
    PROVIDER_REGISTRY,
    PROVIDER_URLS,
    TOOL_CHOICE_PROVIDERS,
    ProviderSpec,
    get_max_tokens,
    get_provider_base_url,
    get_provider_manifest,
    get_provider_spec,
    normalize_provider,
)
from app.services.llm.types import LLMMessage, LLMResponse as LLMResponse, LLMStreamChunk as LLMStreamChunk

__all__ = [
    "MAX_TOKENS_BY_MODEL",
    "MAX_TOKENS_BY_PROVIDER",
    "PROVIDER_ALIASES",
    "PROVIDER_CLIENTS",
    "PROVIDER_REGISTRY",
    "PROVIDER_URLS",
    "TOOL_CHOICE_PROVIDERS",
    "AnthropicClient",
    "ChunkCallback",
    "GeminiClient",
    "LLMClient",
    "LLMError",
    "LLMMessage",
    "LLMResponse",
    "LLMStreamChunk",
    "OpenAICompatibleClient",
    "OpenAIResponsesClient",
    "ProviderSpec",
    "ThinkingCallback",
    "ToolCallback",
    "chat_complete",
    "chat_stream",
    "create_llm_client",
    "get_max_tokens",
    "get_provider_base_url",
    "get_provider_manifest",
    "get_provider_spec",
    "normalize_provider",
]
