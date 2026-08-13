from dataclasses import fields, is_dataclass
from typing import Final

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
    "get_max_tokens",
    "get_model_api_key",
    "get_provider_base_url",
    "get_provider_manifest",
)

UTILS_EXPORTS: Final = (
    "get_tool_params",
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
    "chat_complete",
    "chat_stream",
    "ProviderSpec",
    "PROVIDER_ALIASES",
    "PROVIDER_REGISTRY",
    "PROVIDER_URLS",
    "ANTHROPIC_API_PROVIDERS",
    "TOOL_CHOICE_PROVIDERS",
    "normalize_provider",
    "get_provider_spec",
    "get_provider_manifest",
)

CLIENT_PUBLIC_SYMBOLS: Final = (
    "LLMMessage",
    "LLMResponse",
    "LLMStreamChunk",
    "ChunkCallback",
    "ToolCallback",
    "ThinkingCallback",
    "LLMClient",
    "OpenAICompatibleClient",
    "OpenAIResponsesClient",
    "GeminiClient",
    "AnthropicClient",
    "ProviderSpec",
    "PROVIDER_ALIASES",
    "PROVIDER_REGISTRY",
    "PROVIDER_CLIENTS",
    "PROVIDER_URLS",
    "TOOL_CHOICE_PROVIDERS",
    "MAX_TOKENS_BY_PROVIDER",
    "MAX_TOKENS_BY_MODEL",
    "LLMError",
    "normalize_provider",
    "get_provider_spec",
    "get_provider_manifest",
    "get_provider_base_url",
    "get_max_tokens",
    "create_llm_client",
    "chat_complete",
    "chat_stream",
)

PROVIDER_KEYS: Final = (
    "anthropic",
    "openai",
    "openai-response",
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
    from app.services.llm import client, utils

    assert tuple(llm.__all__) == PACKAGE_EXPORTS
    for name in PACKAGE_EXPORTS:
        assert getattr(llm, name) is not None

    for name in ("LLMClient", "LLMResponse", "LLMError", "LLMMessage"):
        assert getattr(llm, name) is getattr(client, name)

    for name in (
        "create_llm_client",
        "get_max_tokens",
        "get_model_api_key",
        "get_provider_base_url",
        "get_provider_manifest",
    ):
        assert getattr(llm, name) is getattr(utils, name)


def test_client_module_public_contract_symbols_are_importable():
    from app.services.llm import client

    for name in CLIENT_PUBLIC_SYMBOLS:
        assert getattr(client, name) is not None


def test_utils_exports_current_client_and_compatibility_surface():
    from app.services.llm import client, utils

    assert tuple(utils.__all__) == UTILS_EXPORTS
    for name in UTILS_EXPORTS:
        assert getattr(utils, name) is not None

    for name in (
        "LLMClient",
        "OpenAICompatibleClient",
        "OpenAIResponsesClient",
        "GeminiClient",
        "AnthropicClient",
        "LLMMessage",
        "LLMResponse",
        "LLMStreamChunk",
        "LLMError",
        "ProviderSpec",
        "PROVIDER_ALIASES",
        "PROVIDER_REGISTRY",
        "PROVIDER_URLS",
        "TOOL_CHOICE_PROVIDERS",
        "create_llm_client",
        "chat_complete",
        "chat_stream",
        "get_max_tokens",
        "get_provider_manifest",
        "get_provider_base_url",
        "get_provider_spec",
        "normalize_provider",
    ):
        assert getattr(utils, name) is getattr(client, name)

    expected_anthropic_api_providers = {"anthropic"}
    assert expected_anthropic_api_providers == utils.ANTHROPIC_API_PROVIDERS
    assert utils.get_tool_params("openai") == {"tool_choice": "auto", "parallel_tool_calls": True}
    assert utils.get_tool_params("anthropic") == {}


def test_provider_registry_keys_and_provider_spec_shape():
    from app.services.llm import client

    assert tuple(client.PROVIDER_REGISTRY) == PROVIDER_KEYS
    assert is_dataclass(client.ProviderSpec)
    assert tuple(field.name for field in fields(client.ProviderSpec)) == (
        "provider",
        "display_name",
        "protocol",
        "default_base_url",
        "supports_tool_choice",
        "default_max_tokens",
        "model_max_tokens",
    )

    openai = client.PROVIDER_REGISTRY["openai"]
    assert openai.provider == "openai"
    assert openai.display_name == "OpenAI"
    assert openai.protocol == "openai_compatible"
    assert openai.default_base_url == "https://api.openai.com/v1"
    assert openai.supports_tool_choice is True
    assert openai.default_max_tokens == 16384
    assert openai.model_max_tokens == {}

    qwen = client.PROVIDER_REGISTRY["qwen"]
    assert qwen.model_max_tokens == {
        "qwen-plus": 16384,
        "qwen-long": 16384,
        "qwen-turbo": 8192,
        "qwen-max": 8192,
    }

    first = client.ProviderSpec(
        provider="one",
        display_name="One",
        protocol="openai_compatible",
        default_base_url=None,
    )
    second = client.ProviderSpec(
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
    from app.services.llm import client

    assert client.PROVIDER_ALIASES == {
        "openai_response": "openai-response",
        "openairesponses": "openai-response",
    }
    assert client.normalize_provider(" OpenAI_Response ") == "openai-response"
    assert client.get_provider_spec("openairesponses") is client.PROVIDER_REGISTRY["openai-response"]

    assert client.PROVIDER_CLIENTS["anthropic"] is client.AnthropicClient
    assert client.PROVIDER_CLIENTS["openai"] is client.OpenAICompatibleClient
    assert client.PROVIDER_CLIENTS["openai-response"] is client.OpenAIResponsesClient
    assert client.PROVIDER_CLIENTS["gemini"] is client.GeminiClient
    assert client.PROVIDER_CLIENTS["custom"] is client.OpenAICompatibleClient

    expected_provider_urls = {key: spec.default_base_url for key, spec in client.PROVIDER_REGISTRY.items()}
    assert expected_provider_urls == client.PROVIDER_URLS
    expected_provider_max_tokens = {key: spec.default_max_tokens for key, spec in client.PROVIDER_REGISTRY.items()}
    assert expected_provider_max_tokens == client.MAX_TOKENS_BY_PROVIDER
    assert client.MAX_TOKENS_BY_MODEL == {
        "qwen-plus": 16384,
        "qwen-long": 16384,
        "qwen-turbo": 8192,
        "qwen-max": 8192,
    }
    assert "anthropic" not in client.TOOL_CHOICE_PROVIDERS
    assert "baidu" not in client.TOOL_CHOICE_PROVIDERS
    assert "openai" in client.TOOL_CHOICE_PROVIDERS

    manifest_by_provider = {item["provider"]: item for item in client.get_provider_manifest()}
    assert tuple(manifest_by_provider) == PROVIDER_KEYS
    assert manifest_by_provider["openai-response"]["aliases"] == ["openai_response", "openairesponses"]


def test_create_llm_client_factory_returns_current_provider_classes_and_flags():
    from app.services.llm import client

    anthropic = client.create_llm_client("anthropic", "secret", "claude", timeout=7)
    assert type(anthropic) is client.AnthropicClient
    assert anthropic.base_url == "https://api.anthropic.com"
    assert anthropic.model == "claude"
    assert anthropic.timeout == 7

    responses = client.create_llm_client("openai_response", "secret", "gpt-4.1")
    assert type(responses) is client.OpenAIResponsesClient
    assert responses.base_url == "https://api.openai.com/v1"

    gemini = client.create_llm_client("gemini", "secret", "gemini-2.5-pro")
    assert type(gemini) is client.GeminiClient
    assert gemini.base_url == "https://generativelanguage.googleapis.com/v1beta"

    qwen = client.create_llm_client("qwen", "secret", "qwen-plus")
    assert type(qwen) is client.OpenAICompatibleClient
    assert qwen.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert qwen.supports_tool_choice is True
    assert qwen.supports_cache_control is True

    baidu = client.create_llm_client("baidu", "secret", "ernie")
    assert type(baidu) is client.OpenAICompatibleClient
    assert baidu.base_url == "https://qianfan.baidubce.com/v2"
    assert baidu.supports_tool_choice is False
    assert baidu.supports_cache_control is False


def test_get_max_tokens_preserves_current_priority_order():
    from app.services.llm import client

    cases = (
        ("qwen", "qwen-plus-latest", None, 16384),
        ("qwen", "qwen-turbo", None, 8192),
        ("openai", "gpt-4.1", None, 16384),
        ("__no_such_provider__", "qwen-plus-latest", None, 16384),
        ("__no_such_provider__", "unknown-model", None, 4096),
        ("openai", "gpt-4.1", 777, 777),
    )
    for provider, model, override, expected in cases:
        assert client.get_max_tokens(provider, model, override) == expected


def test_unknown_provider_falls_back_to_openai_compatible_client_and_base_url():
    from app.services.llm import client
    from app.services.llm.utils import create_llm_client as utils_create_llm_client

    fallback = client.create_llm_client(
        provider="__no_such_provider__",
        api_key="secret",
        model="custom-model",
        timeout=9,
    )
    assert type(fallback) is client.OpenAICompatibleClient
    assert fallback.api_key == "secret"
    assert fallback.model == "custom-model"
    assert fallback.timeout == 9
    assert fallback.base_url == "https://api.openai.com/v1"
    assert fallback.supports_tool_choice is True
    assert fallback.supports_cache_control is False

    custom = utils_create_llm_client(
        provider="__no_such_provider__",
        api_key="secret",
        model="custom-model",
        base_url="https://gateway.example/v42",
    )
    assert type(custom) is client.OpenAICompatibleClient
    assert custom.base_url == "https://gateway.example/v42"
    assert custom.supports_tool_choice is True
    assert custom.supports_cache_control is False
    assert client.get_provider_base_url("__no_such_provider__") is None
