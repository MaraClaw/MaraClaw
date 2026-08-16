"""LLM provider registry and compatibility constants."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TypedDict

from app.services.llm.base import LLMClient
from app.services.llm.providers import (
    AnthropicClient,
    GeminiClient,
    OpenAICompatibleClient,
    OpenAIResponsesClient,
)

type ProviderProtocol = Literal["openai_compatible", "anthropic", "openai_responses", "gemini"]


class ProviderManifest(TypedDict):
    """Provider capabilities exposed for UI and configuration discovery."""

    provider: str
    display_name: str
    protocol: ProviderProtocol
    default_base_url: str | None
    default_model: str | None
    reasoning_efforts: list[str]
    supports_tool_choice: bool
    default_max_tokens: int
    model_max_tokens: dict[str, int]
    aliases: list[str]


@dataclass(frozen=True)
class ProviderSpec:
    """Provider registry entry."""

    provider: str
    display_name: str
    protocol: ProviderProtocol
    default_base_url: str | None
    default_model: str | None = None
    supports_tool_choice: bool = True
    default_max_tokens: int = 4096
    model_max_tokens: dict[str, int] = field(default_factory=dict[str, int])


PROVIDER_ALIASES: dict[str, str] = {
    "openai_response": "openai-response",
    "openairesponses": "openai-response",
    "xai": "grok",
    "x-ai": "grok",
    "x_ai": "grok",
    "zai": "zhipu",
    "z.ai": "zhipu",
    "z_ai": "zhipu",
}


PROVIDER_REGISTRY: dict[str, ProviderSpec] = {
    "anthropic": ProviderSpec(
        provider="anthropic",
        display_name="Anthropic",
        protocol="anthropic",
        default_base_url="https://api.anthropic.com",
        default_model="claude-opus-5",
        supports_tool_choice=False,
        default_max_tokens=8192,
    ),
    "openai": ProviderSpec(
        provider="openai",
        display_name="OpenAI",
        protocol="openai_compatible",
        default_base_url="https://api.openai.com/v1",
        default_model="gpt-5.6",
        default_max_tokens=16384,
    ),
    "openai-response": ProviderSpec(
        provider="openai-response",
        display_name="OpenAI Responses",
        protocol="openai_responses",
        default_base_url="https://api.openai.com/v1",
        default_model="gpt-5.6",
        default_max_tokens=16384,
    ),
    "grok": ProviderSpec(
        provider="grok",
        display_name="Grok (xAI)",
        protocol="openai_compatible",
        default_base_url="https://api.x.ai/v1",
        default_model="grok-4.6",
        default_max_tokens=16384,
    ),
    "azure": ProviderSpec(
        provider="azure",
        display_name="Azure OpenAI",
        protocol="openai_compatible",
        default_base_url=None,
        default_model="gpt-5.6",
        default_max_tokens=16384,
    ),
    "deepseek": ProviderSpec(
        provider="deepseek",
        display_name="DeepSeek",
        protocol="openai_compatible",
        default_base_url="https://api.deepseek.com/v1",
        default_model="deepseek-v4-pro",
        default_max_tokens=8192,
    ),
    "qwen": ProviderSpec(
        provider="qwen",
        display_name="Qwen (DashScope)",
        protocol="openai_compatible",
        default_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        default_model="qwen3.7-max",
        default_max_tokens=8192,
        model_max_tokens={
            "qwen-plus": 16384,
            "qwen-long": 16384,
            "qwen-turbo": 8192,
            "qwen-max": 8192,
            "qwen3.7-max": 16384,
        },
    ),
    "minimax": ProviderSpec(
        provider="minimax",
        display_name="MiniMax",
        protocol="openai_compatible",
        default_base_url="https://api.minimaxi.com/v1",
        default_model="MiniMax-M3",
        default_max_tokens=16384,
    ),
    "openrouter": ProviderSpec(
        provider="openrouter",
        display_name="OpenRouter",
        protocol="openai_compatible",
        default_base_url="https://openrouter.ai/api/v1",
        default_model="openrouter/auto",
        default_max_tokens=4096,
    ),
    "zhipu": ProviderSpec(
        provider="zhipu",
        display_name="Z.ai",
        protocol="openai_compatible",
        default_base_url="https://open.bigmodel.cn/api/paas/v4",
        default_model="glm-5.3",
        default_max_tokens=8192,
    ),
    "baidu": ProviderSpec(
        provider="baidu",
        display_name="Baidu (Qianfan)",
        protocol="openai_compatible",
        default_base_url="https://qianfan.baidubce.com/v2",
        default_model="ernie-5.0",
        supports_tool_choice=False,
        default_max_tokens=4096,
    ),
    "gemini": ProviderSpec(
        provider="gemini",
        display_name="Gemini",
        protocol="gemini",
        default_base_url="https://generativelanguage.googleapis.com/v1beta",
        default_model="gemini-3.7-flash",
        default_max_tokens=8192,
    ),
    "kimi": ProviderSpec(
        provider="kimi",
        display_name="Kimi (Moonshot)",
        protocol="openai_compatible",
        default_base_url="https://api.moonshot.cn/v1",
        default_model="kimi-k3",
        default_max_tokens=8192,
    ),
    "vllm": ProviderSpec(
        provider="vllm",
        display_name="vLLM",
        protocol="openai_compatible",
        default_base_url="http://localhost:8000/v1",
        default_max_tokens=4096,
    ),
    "ollama": ProviderSpec(
        provider="ollama",
        display_name="Ollama",
        protocol="openai_compatible",
        default_base_url="http://localhost:11434/v1",
        default_max_tokens=4096,
    ),
    "sglang": ProviderSpec(
        provider="sglang",
        display_name="SGLang",
        protocol="openai_compatible",
        default_base_url="http://localhost:30000/v1",
        default_max_tokens=4096,
    ),
    "custom": ProviderSpec(
        provider="custom",
        display_name="Custom",
        protocol="openai_compatible",
        default_base_url=None,
        default_max_tokens=4096,
    ),
}


def normalize_provider(provider: str) -> str:
    """Normalize provider id with aliases and lowercase."""
    normalized = (provider or "").strip().lower()
    return PROVIDER_ALIASES.get(normalized, normalized)


def get_provider_spec(provider: str) -> ProviderSpec | None:
    """Get provider spec from registry."""
    return PROVIDER_REGISTRY.get(normalize_provider(provider))


def get_provider_manifest() -> list[ProviderManifest]:
    """List supported providers and capabilities for UI/config discovery."""
    from app.services.llm.reasoning import supported_reasoning_efforts

    return [
        {
            "provider": spec.provider,
            "display_name": spec.display_name,
            "protocol": spec.protocol,
            "default_base_url": spec.default_base_url,
            "default_model": spec.default_model,
            "reasoning_efforts": list(supported_reasoning_efforts(spec.provider)),
            "supports_tool_choice": spec.supports_tool_choice,
            "default_max_tokens": spec.default_max_tokens,
            "model_max_tokens": spec.model_max_tokens,
            "aliases": [alias for alias, provider in PROVIDER_ALIASES.items() if provider == spec.provider],
        }
        for spec in PROVIDER_REGISTRY.values()
    ]


_PROTOCOL_CLIENTS: dict[
    ProviderProtocol,
    type[LLMClient],
] = {
    "openai_compatible": OpenAICompatibleClient,
    "anthropic": AnthropicClient,
    "openai_responses": OpenAIResponsesClient,
    "gemini": GeminiClient,
}

PROVIDER_CLIENTS: dict[str, type[LLMClient]] = {
    spec.provider: _PROTOCOL_CLIENTS[spec.protocol] for spec in PROVIDER_REGISTRY.values()
}

PROVIDER_URLS: dict[str, str | None] = {spec.provider: spec.default_base_url for spec in PROVIDER_REGISTRY.values()}

TOOL_CHOICE_PROVIDERS: set[str] = {spec.provider for spec in PROVIDER_REGISTRY.values() if spec.supports_tool_choice}

MAX_TOKENS_BY_PROVIDER: dict[str, int] = {spec.provider: spec.default_max_tokens for spec in PROVIDER_REGISTRY.values()}

MAX_TOKENS_BY_MODEL: dict[str, int] = {
    prefix: limit for spec in PROVIDER_REGISTRY.values() for prefix, limit in spec.model_max_tokens.items()
}


def get_provider_base_url(provider: str, custom_base_url: str | None = None) -> str | None:
    """Return the API base URL for a provider."""
    if custom_base_url:
        return custom_base_url
    spec = get_provider_spec(provider)
    if spec:
        return spec.default_base_url
    return PROVIDER_URLS.get(normalize_provider(provider))


def get_max_tokens(provider: str, model: str | None = None, max_output_tokens: int | None = None) -> int:
    """Return a safe max_tokens value for the given provider/model pair."""
    spec = get_provider_spec(provider)
    model_limits = spec.model_max_tokens if spec else MAX_TOKENS_BY_MODEL

    if isinstance(max_output_tokens, int) and max_output_tokens > 0:
        return max_output_tokens

    if model:
        for prefix, limit in model_limits.items():
            if model.lower().startswith(prefix):
                return limit

    if spec:
        return spec.default_max_tokens

    return MAX_TOKENS_BY_PROVIDER.get(normalize_provider(provider), 4096)


__all__ = [
    "MAX_TOKENS_BY_MODEL",
    "MAX_TOKENS_BY_PROVIDER",
    "PROVIDER_ALIASES",
    "PROVIDER_CLIENTS",
    "PROVIDER_REGISTRY",
    "PROVIDER_URLS",
    "TOOL_CHOICE_PROVIDERS",
    "ProviderSpec",
    "get_max_tokens",
    "get_provider_base_url",
    "get_provider_manifest",
    "get_provider_spec",
    "normalize_provider",
]
