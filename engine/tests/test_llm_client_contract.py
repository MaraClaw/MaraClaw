from dataclasses import fields, is_dataclass
from importlib import import_module
from typing import Final

import pytest

from app.services.llm import base, factory, providers, registry, types, utils

PACKAGE_EXPORTS: Final = (
    "call_llm",
    "call_llm_with_failover",
    "call_agent_llm",
    "call_agent_llm_with_tools",
    "FailoverGuard",
    "is_retryable_error",
    "classify_error",
    "FailoverErrorType",
    "LLMClient",
    "LLMResponse",
    "LLMError",
    "LLMMessage",
    "create_llm_client",
    "create_llm_client_from_model",
    "create_fresh_llm_client_from_model",
    "ensure_fresh_pool_model",
    "get_max_tokens",
    "get_model_api_key",
    "get_provider_base_url",
    "get_provider_manifest",
)

UTILS_EXPORTS: Final = (
    "get_provider_base_url",
    "get_max_tokens",
    "get_model_api_key",
    "convert_chat_messages_to_llm_format",
    "truncate_messages_with_pair_integrity",
    "LLMClient",
    "OpenAICompatibleClient",
    "OpenAIResponsesClient",
    "GeminiClient",
    "AnthropicClient",
    "LLMMessage",
    "LLMResponse",
    "LLMStreamChunk",
    "LLMError",
    "create_llm_client",
    "create_llm_client_from_model",
    "create_fresh_llm_client_from_model",
    "ensure_fresh_pool_model",
    "chat_complete",
    "chat_stream",
    "ProviderSpec",
    "PROVIDER_ALIASES",
    "PROVIDER_REGISTRY",
    "PROVIDER_URLS",
    "TOOL_CHOICE_PROVIDERS",
    "normalize_provider",
    "get_provider_spec",
    "get_provider_manifest",
)

PROVIDER_KEYS: Final = (
    "anthropic",
    "openai",
    "openai-response",
    "grok",
    "azure",
    "deepseek",
    "qwen",
    "minimax",
    "openrouter",
    "zhipu",
    "baidu",
    "gemini",
    "kimi",
    "vllm",
    "ollama",
    "sglang",
    "custom",
)


def test_package_facade_exports_current_public_symbols():
    from app.services import llm

    assert tuple(llm.__all__) == PACKAGE_EXPORTS
    for name in PACKAGE_EXPORTS:
        assert getattr(llm, name) is not None

    for name in ("LLMClient", "LLMError"):
        assert getattr(llm, name) is getattr(base, name)
    for name in ("LLMResponse", "LLMMessage"):
        assert getattr(llm, name) is getattr(types, name)

    for name in (
        "create_llm_client",
        "create_llm_client_from_model",
        "create_fresh_llm_client_from_model",
        "ensure_fresh_pool_model",
        "get_max_tokens",
        "get_model_api_key",
        "get_provider_base_url",
        "get_provider_manifest",
    ):
        assert getattr(llm, name) is getattr(utils, name)


def test_client_module_is_retired() -> None:
    with pytest.raises(ModuleNotFoundError) as exc:
        import_module("app.services.llm.client")
    assert exc.value.name == "app.services.llm.client"


def test_utils_exports_current_owned_module_surface():
    assert tuple(utils.__all__) == UTILS_EXPORTS
    for name in UTILS_EXPORTS:
        assert getattr(utils, name) is not None

    for owner, names in (
        (base, ("LLMClient", "LLMError")),
        (providers, ("OpenAICompatibleClient", "OpenAIResponsesClient", "GeminiClient", "AnthropicClient")),
        (types, ("LLMMessage", "LLMResponse", "LLMStreamChunk")),
        (factory, ("create_llm_client", "chat_complete", "chat_stream")),
        (
            registry,
            (
                "ProviderSpec",
                "PROVIDER_ALIASES",
                "PROVIDER_REGISTRY",
                "PROVIDER_URLS",
                "TOOL_CHOICE_PROVIDERS",
                "get_max_tokens",
                "get_provider_manifest",
                "get_provider_base_url",
                "get_provider_spec",
                "normalize_provider",
            ),
        ),
    ):
        for name in names:
            assert getattr(utils, name) is getattr(owner, name)

    assert not hasattr(utils, "ANTHROPIC_API_PROVIDERS")
    assert not hasattr(utils, "get_tool_params")


def test_provider_registry_keys_and_provider_spec_shape():
    assert tuple(registry.PROVIDER_REGISTRY) == PROVIDER_KEYS
    assert is_dataclass(registry.ProviderSpec)
    assert tuple(field.name for field in fields(registry.ProviderSpec)) == (
        "provider",
        "display_name",
        "protocol",
        "default_base_url",
        "default_model",
        "supports_tool_choice",
        "default_max_tokens",
        "model_max_tokens",
    )

    openai = registry.PROVIDER_REGISTRY["openai"]
    assert openai.provider == "openai"
    assert openai.display_name == "OpenAI"
    assert openai.protocol == "openai_compatible"
    assert openai.default_base_url == "https://api.openai.com/v1"
    assert openai.default_model == "gpt-5.6"
    assert openai.supports_tool_choice is True
    assert openai.default_max_tokens == 16384
    assert openai.model_max_tokens == {}

    qwen = registry.PROVIDER_REGISTRY["qwen"]
    assert qwen.model_max_tokens == {
        "qwen-plus": 16384,
        "qwen-long": 16384,
        "qwen-turbo": 8192,
        "qwen-max": 8192,
        "qwen3.7-max": 16384,
    }

    first = registry.ProviderSpec(
        provider="one",
        display_name="One",
        protocol="openai_compatible",
        default_base_url=None,
    )
    second = registry.ProviderSpec(
        provider="two",
        display_name="Two",
        protocol="gemini",
        default_base_url="https://example.invalid",
    )
    assert first.supports_tool_choice is True
    assert first.default_max_tokens == 4096
    assert first.model_max_tokens == {}
    assert first.model_max_tokens is not second.model_max_tokens


def test_provider_registry_derivatives_and_aliases_match_current_behavior():
    assert registry.PROVIDER_ALIASES == {
        "openai_response": "openai-response",
        "openairesponses": "openai-response",
        "xai": "grok",
        "x-ai": "grok",
        "x_ai": "grok",
        "zai": "zhipu",
        "z.ai": "zhipu",
        "z_ai": "zhipu",
    }
    assert registry.normalize_provider(" OpenAI_Response ") == "openai-response"
    assert registry.get_provider_spec("openairesponses") is registry.PROVIDER_REGISTRY["openai-response"]
    assert registry.normalize_provider("XAI") == "grok"
    assert registry.get_provider_spec("x-ai") is registry.PROVIDER_REGISTRY["grok"]
    assert registry.normalize_provider("Z.AI") == "zhipu"
    assert registry.get_provider_spec("zai") is registry.PROVIDER_REGISTRY["zhipu"]

    assert registry.PROVIDER_CLIENTS["anthropic"] is providers.AnthropicClient
    assert registry.PROVIDER_CLIENTS["openai"] is providers.OpenAICompatibleClient
    assert registry.PROVIDER_CLIENTS["openai-response"] is providers.OpenAIResponsesClient
    assert registry.PROVIDER_CLIENTS["gemini"] is providers.GeminiClient
    assert registry.PROVIDER_CLIENTS["custom"] is providers.OpenAICompatibleClient
    assert registry.PROVIDER_CLIENTS["grok"] is providers.OpenAICompatibleClient

    expected_provider_urls = {key: spec.default_base_url for key, spec in registry.PROVIDER_REGISTRY.items()}
    assert expected_provider_urls == registry.PROVIDER_URLS
    expected_provider_max_tokens = {key: spec.default_max_tokens for key, spec in registry.PROVIDER_REGISTRY.items()}
    assert expected_provider_max_tokens == registry.MAX_TOKENS_BY_PROVIDER
    assert registry.MAX_TOKENS_BY_MODEL == {
        "qwen-plus": 16384,
        "qwen-long": 16384,
        "qwen-turbo": 8192,
        "qwen-max": 8192,
        "qwen3.7-max": 16384,
    }
    assert "anthropic" not in registry.TOOL_CHOICE_PROVIDERS
    assert "baidu" not in registry.TOOL_CHOICE_PROVIDERS
    assert "openai" in registry.TOOL_CHOICE_PROVIDERS

    manifest_by_provider = {item["provider"]: item for item in registry.get_provider_manifest()}
    assert tuple(manifest_by_provider) == PROVIDER_KEYS
    assert manifest_by_provider["openai-response"]["aliases"] == ["openai_response", "openairesponses"]
    assert manifest_by_provider["grok"]["display_name"] == "Grok (xAI)"
    assert manifest_by_provider["grok"]["default_base_url"] == "https://api.x.ai/v1"
    assert manifest_by_provider["grok"]["default_model"] == "grok-4.6"
    assert manifest_by_provider["grok"]["aliases"] == ["xai", "x-ai", "x_ai"]
    assert manifest_by_provider["zhipu"]["display_name"] == "Z.ai"
    assert manifest_by_provider["zhipu"]["default_model"] == "glm-5.3"
    assert manifest_by_provider["zhipu"]["default_base_url"] == "https://api.z.ai/api/paas/v4"
    assert manifest_by_provider["zhipu"]["aliases"] == ["zai", "z.ai", "z_ai"]
    assert manifest_by_provider["openai"]["default_model"] == "gpt-5.6"
    assert manifest_by_provider["anthropic"]["default_model"] == "claude-opus-5"
    assert manifest_by_provider["grok"]["reasoning_efforts"] == ["none", "low", "medium", "high", "xhigh"]
    assert manifest_by_provider["gemini"]["reasoning_efforts"] == ["none", "low", "medium", "high"]
    assert manifest_by_provider["ollama"]["reasoning_efforts"] == []


def test_create_llm_client_factory_returns_current_provider_classes_and_flags():
    anthropic = factory.create_llm_client("anthropic", "secret", "claude", timeout=7)
    assert type(anthropic) is providers.AnthropicClient
    assert anthropic.base_url == "https://api.anthropic.com"
    assert anthropic.model == "claude"
    assert anthropic.timeout == 7

    responses = factory.create_llm_client("openai_response", "secret", "gpt-4.1")
    assert type(responses) is providers.OpenAIResponsesClient
    assert responses.base_url == "https://api.openai.com/v1"

    gemini = factory.create_llm_client("gemini", "secret", "gemini-2.5-pro")
    assert type(gemini) is providers.GeminiClient
    assert gemini.base_url == "https://generativelanguage.googleapis.com/v1beta"

    grok = factory.create_llm_client("xai", "secret", "grok-4")
    assert type(grok) is providers.OpenAICompatibleClient
    assert grok.base_url == "https://api.x.ai/v1"
    assert grok.model == "grok-4"
    assert grok.supports_tool_choice is True

    qwen = factory.create_llm_client("qwen", "secret", "qwen-plus")
    assert type(qwen) is providers.OpenAICompatibleClient
    assert qwen.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert qwen.supports_tool_choice is True
    assert qwen.supports_cache_control is True

    baidu = factory.create_llm_client("baidu", "secret", "ernie")
    assert type(baidu) is providers.OpenAICompatibleClient
    assert baidu.base_url == "https://qianfan.baidubce.com/v2"
    assert baidu.supports_tool_choice is False
    assert baidu.supports_cache_control is False


def test_get_max_tokens_preserves_current_priority_order():
    cases = (
        ("qwen", "qwen-plus-latest", None, 16384),
        ("qwen", "qwen-turbo", None, 8192),
        ("openai", "gpt-4.1", None, 16384),
        ("__no_such_provider__", "qwen-plus-latest", None, 16384),
        ("__no_such_provider__", "unknown-model", None, 4096),
        ("openai", "gpt-4.1", 777, 777),
    )
    for provider, model, override, expected in cases:
        assert registry.get_max_tokens(provider, model, override) == expected


@pytest.mark.parametrize("base_url", [None, "https://gateway.example/v42"])
def test_unknown_provider_is_rejected_with_any_endpoint(base_url: str | None) -> None:
    with pytest.raises(factory.UnknownProviderError) as exc:
        utils.create_llm_client("__no_such_provider__", "secret", "custom-model", base_url=base_url, timeout=9)
    assert exc.value.provider == "__no_such_provider__"
    assert registry.get_provider_base_url("__no_such_provider__") is None


def test_registered_custom_provider_uses_explicit_endpoint() -> None:
    custom = utils.create_llm_client(
        provider="custom",
        api_key="secret",
        model="custom-model",
        base_url="https://gateway.example/v42",
        timeout=9,
    )
    assert type(custom) is providers.OpenAICompatibleClient
    assert custom.api_key == "secret"
    assert custom.model == "custom-model"
    assert custom.timeout == 9
    assert custom.base_url == "https://gateway.example/v42"
    assert custom.supports_tool_choice is True
    assert custom.supports_cache_control is False
