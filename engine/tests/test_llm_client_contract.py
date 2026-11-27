import asyncio
from collections.abc import Iterator
from dataclasses import fields, is_dataclass
from importlib import import_module
from types import SimpleNamespace
from typing import Any, Final
from uuid import UUID

import httpx
import pydantic
import pytest
from tests.llm_retry_fakes import FakeClock, health_model, install_provider_health, trip

from app.config import Settings
from app.services.llm import base, caller, factory, provider_health, providers, registry, types, utils
from app.services.llm.failover import FailoverErrorType, classify_error
from app.services.llm.provider_health import (
    Admission,
    ProviderHealth,
    ProviderUnavailableError,
    ensure_available,
    guarded,
    model_key,
)
from app.services.llm.retry_policy import LLMHTTPError

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
    assert openai.default_model == "gpt-6.2-sol"
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
    assert manifest_by_provider["grok"]["default_model"] == "grok-4.8"
    assert manifest_by_provider["grok"]["aliases"] == ["xai", "x-ai", "x_ai"]
    assert manifest_by_provider["zhipu"]["display_name"] == "Z.ai"
    assert manifest_by_provider["zhipu"]["default_model"] == "glm-5.3"
    assert manifest_by_provider["zhipu"]["default_base_url"] == "https://api.z.ai/api/paas/v4"
    assert manifest_by_provider["zhipu"]["aliases"] == ["zai", "z.ai", "z_ai"]
    assert manifest_by_provider["openai"]["default_model"] == "gpt-6.2-sol"
    assert manifest_by_provider["anthropic"]["default_model"] == "claude-opus-5-5"
    expected_efforts = ["none", "low", "medium", "high", "xhigh", "max", "ultra"]
    for provider in ("openai", "openai-response", "azure", "openrouter", "anthropic", "deepseek"):
        assert manifest_by_provider[provider]["reasoning_efforts"] == expected_efforts
    assert manifest_by_provider["grok"]["reasoning_efforts"] == ["none", "low", "medium", "high", "xhigh"]
    assert manifest_by_provider["qwen"]["reasoning_efforts"] == ["none", "low", "medium", "high", "xhigh"]
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


KEY: Final = UUID(int=7)
OTHER_KEY: Final = UUID(int=8)


@pytest.fixture
def circuit() -> tuple[ProviderHealth, FakeClock]:
    clock = FakeClock()
    return ProviderHealth(failure_threshold=3, cooldown=30.0, clock=clock), clock


def _open_circuit(tracker: ProviderHealth, key: UUID = KEY) -> None:
    for _ in range(3):
        _ = tracker.failed(key, Admission.CALL)


@pytest.mark.parametrize("status", [429, 500, 501, 502, 503, 504, 529, 599])
def test_rate_limit_and_server_statuses_are_retryable_whatever_the_body_says(status: int) -> None:
    assert classify_error(LLMHTTPError(status, "bad request: invalid schema, unauthorized")) is (
        FailoverErrorType.RETRYABLE
    )


@pytest.mark.parametrize("status", [400, 401, 403, 404, 408, 409, 413, 422])
def test_other_client_statuses_are_never_retryable_whatever_the_body_says(status: int) -> None:
    body = "request timeout: rate limit hit, connection reset, 503 service unavailable"
    assert classify_error(LLMHTTPError(status, body)) is FailoverErrorType.NON_RETRYABLE


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("[LLM Error] HTTP 400: request timeout", FailoverErrorType.RETRYABLE),
        ("[LLM Error] HTTP 401: unauthorized", FailoverErrorType.NON_RETRYABLE),
        ("Connection failed after 3 attempts: boom", FailoverErrorType.RETRYABLE),
        ("API error: rate limit exceeded", FailoverErrorType.RETRYABLE),
        ("API error: invalid request schema", FailoverErrorType.NON_RETRYABLE),
        ("something odd happened", FailoverErrorType.UNKNOWN),
    ],
)
def test_errors_without_a_status_keep_their_message_classification(message: str, expected: FailoverErrorType) -> None:
    assert classify_error(Exception(message)) is expected


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (httpx.ReadTimeout(""), FailoverErrorType.RETRYABLE),
        (httpx.PoolTimeout(""), FailoverErrorType.RETRYABLE),
        (TimeoutError(), FailoverErrorType.RETRYABLE),
        (ValueError(""), FailoverErrorType.UNKNOWN),
        (httpx.ReadTimeout("invalid request schema"), FailoverErrorType.NON_RETRYABLE),
    ],
)
def test_a_typed_timeout_without_text_is_retryable_but_the_message_still_comes_first(
    error: Exception, expected: FailoverErrorType
) -> None:
    assert classify_error(error) is expected


def test_circuit_opens_after_the_threshold_of_consecutive_failures(circuit: tuple[ProviderHealth, FakeClock]) -> None:
    tracker, _clock = circuit
    assert [tracker.failed(KEY, Admission.CALL) for _ in range(3)] == [False, False, True]
    assert tracker.refusing(KEY)
    assert tracker.admit(KEY) is None
    assert tracker.admit(OTHER_KEY) is Admission.CALL


def test_a_success_resets_the_failure_streak(circuit: tuple[ProviderHealth, FakeClock]) -> None:
    tracker, _clock = circuit
    assert [tracker.failed(KEY, Admission.CALL) for _ in range(2)] == [False, False]
    assert tracker.succeeded(KEY, Admission.CALL) is False
    assert [tracker.failed(KEY, Admission.CALL) for _ in range(2)] == [False, False]
    assert not tracker.refusing(KEY)


def test_exactly_one_probe_is_admitted_after_the_cooldown(circuit: tuple[ProviderHealth, FakeClock]) -> None:
    # Given an open circuit.
    tracker, clock = circuit
    _open_circuit(tracker)
    clock.advance(29.99)
    assert tracker.admit(KEY) is None
    assert tracker.retry_in(KEY) == pytest.approx(0.01)
    # When the cooldown ends, the first caller becomes the probe and nobody else is let through.
    clock.advance(0.01)
    assert not tracker.refusing(KEY)
    assert tracker.admit(KEY) is Admission.PROBE
    assert tracker.refusing(KEY)
    assert [tracker.admit(KEY) for _ in range(3)] == [None, None, None]


def test_a_successful_probe_closes_the_circuit(circuit: tuple[ProviderHealth, FakeClock]) -> None:
    tracker, clock = circuit
    _open_circuit(tracker)
    clock.advance(30)
    probe = tracker.admit(KEY)
    assert probe is Admission.PROBE
    assert tracker.succeeded(KEY, probe) is True
    assert [tracker.admit(KEY) for _ in range(3)] == [Admission.CALL] * 3
    assert tracker.retry_in(KEY) == 0
    assert [tracker.failed(KEY, Admission.CALL) for _ in range(3)] == [False, False, True]


def test_a_failed_probe_reopens_the_circuit_for_another_cooldown(circuit: tuple[ProviderHealth, FakeClock]) -> None:
    tracker, clock = circuit
    _open_circuit(tracker)
    clock.advance(30)
    probe = tracker.admit(KEY)
    assert probe is Admission.PROBE
    clock.advance(4)
    assert tracker.failed(KEY, probe) is True
    assert tracker.admit(KEY) is None
    assert tracker.retry_in(KEY) == pytest.approx(30)
    clock.advance(30)
    assert tracker.admit(KEY) is Admission.PROBE


def test_an_abandoned_probe_hands_the_slot_to_the_next_caller(circuit: tuple[ProviderHealth, FakeClock]) -> None:
    tracker, clock = circuit
    _open_circuit(tracker)
    clock.advance(30)
    probe = tracker.admit(KEY)
    assert probe is Admission.PROBE
    assert tracker.admit(KEY) is None
    tracker.abandoned(KEY, probe)
    assert tracker.admit(KEY) is Admission.PROBE


def test_calls_admitted_before_the_circuit_opened_do_not_move_it(circuit: tuple[ProviderHealth, FakeClock]) -> None:
    # Given a circuit that opened while earlier calls were still in flight.
    tracker, clock = circuit
    _open_circuit(tracker)
    clock.advance(30)
    probe = tracker.admit(KEY)
    assert probe is Admission.PROBE
    # When those stragglers finish, neither their failure nor their success decides the probe.
    assert tracker.failed(KEY, Admission.CALL) is False
    assert tracker.succeeded(KEY, Admission.CALL) is False
    assert tracker.refusing(KEY)
    assert tracker.succeeded(KEY, probe) is True


def test_every_model_has_its_own_circuit(monkeypatch: pytest.MonkeyPatch) -> None:
    tracker, _clock = install_provider_health(monkeypatch)
    sick, well = health_model("sick"), health_model("well")
    trip(tracker, sick)
    with pytest.raises(ProviderUnavailableError):
        ensure_available(sick)
    ensure_available(well)
    assert provider_health.is_unavailable(sick)
    assert not provider_health.is_unavailable(well)


async def _answer() -> str:
    return "answer"


def _fails_with(error: BaseException):
    async def call() -> str:
        raise error

    return call


@pytest.mark.parametrize(
    "error",
    [LLMHTTPError(429, "slow down"), LLMHTTPError(503, "busy"), RuntimeError("503 unavailable"), httpx.ReadTimeout("")],
    ids=["429", "503", "message-503", "typed-timeout"],
)
async def test_retryable_failures_open_the_circuit_and_a_refused_call_never_reaches_the_provider(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    # Given a model whose provider fails with a retryable error.
    _tracker, _clock = install_provider_health(monkeypatch, threshold=2)
    model = health_model()
    for _ in range(2):
        with pytest.raises(type(error)):
            _ = await guarded(model, _fails_with(error))
    reached: list[str] = []

    async def call() -> str:
        reached.append("called")
        return "answer"

    # When the next call is made, it is refused before the provider and says why, so the failover reads it as retryable.
    with pytest.raises(ProviderUnavailableError) as refused:
        _ = await guarded(model, call)
    assert reached == []
    assert str(refused.value) == "LLM provider is unavailable after repeated failures; try again in 30 s"
    assert caller.is_retryable_error(f"[LLM Error] {refused.value}")


@pytest.mark.parametrize("status", [400, 401, 403, 404, 408, 422])
async def test_client_errors_never_open_the_circuit_and_prove_the_provider_is_up(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    # Given a streak one failure short of the threshold, then 4xx answers whose bodies talk about timeouts.
    tracker, _clock = install_provider_health(monkeypatch, threshold=3)
    model = health_model()
    key = model_key(model)
    assert key is not None
    rejected = LLMHTTPError(status, "request timeout; rate limit; connection reset")
    for _ in range(2):
        with pytest.raises(LLMHTTPError):
            _ = await guarded(model, _fails_with(LLMHTTPError(503, "busy")))
    # When more of them arrive, each is an answer: the streak is reset and the circuit stays closed.
    for _ in range(10):
        with pytest.raises(LLMHTTPError):
            _ = await guarded(model, _fails_with(rejected))
    with pytest.raises(LLMHTTPError):
        _ = await guarded(model, _fails_with(LLMHTTPError(503, "busy")))
    assert not tracker.refusing(key)
    assert await guarded(model, _answer) == "answer"


async def test_an_error_that_says_nothing_about_the_provider_neither_counts_nor_resets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker, _clock = install_provider_health(monkeypatch, threshold=2)
    model = health_model()
    key = model_key(model)
    assert key is not None
    with pytest.raises(LLMHTTPError):
        _ = await guarded(model, _fails_with(LLMHTTPError(503, "busy")))
    for _ in range(5):
        with pytest.raises(ValueError, match="bug"):
            _ = await guarded(model, _fails_with(ValueError("bug")))
    assert not tracker.refusing(key)
    with pytest.raises(LLMHTTPError):
        _ = await guarded(model, _fails_with(LLMHTTPError(503, "busy")))
    assert tracker.refusing(key)


async def test_the_probe_decides_and_a_cancelled_probe_frees_the_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given an open circuit past its cooldown, and a probe parked on the provider.
    tracker, clock = install_provider_health(monkeypatch, threshold=2)
    model = health_model()
    trip(tracker, model, failures=2)
    clock.advance(30)
    entered, release = asyncio.Event(), asyncio.Event()

    async def parked() -> str:
        entered.set()
        await release.wait()
        return "answer"

    probe = asyncio.create_task(guarded(model, parked))
    await asyncio.wait_for(entered.wait(), timeout=1)
    # When another call arrives meanwhile, it is refused and the probe slot stays taken.
    with pytest.raises(ProviderUnavailableError, match="try again shortly"):
        _ = await guarded(model, _answer)
    # Then cancelling the probe proves nothing: the next caller becomes the probe and its answer closes the circuit.
    _ = probe.cancel()
    with pytest.raises(asyncio.CancelledError):
        _ = await probe
    assert await guarded(model, _answer) == "answer"
    assert not provider_health.is_unavailable(model)
    assert await guarded(model, _answer) == "answer"


async def test_a_failed_probe_reopens_and_a_peek_does_not_take_the_probe_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    tracker, clock = install_provider_health(monkeypatch, threshold=2)
    model = health_model()
    key = model_key(model)
    assert key is not None
    trip(tracker, model, failures=2)
    clock.advance(30)
    ensure_available(model)
    ensure_available(model)
    with pytest.raises(LLMHTTPError):
        _ = await guarded(model, _fails_with(LLMHTTPError(500, "still down")))
    assert tracker.refusing(key)
    with pytest.raises(ProviderUnavailableError, match="try again in 30 s"):
        ensure_available(model)


async def test_a_model_without_a_row_id_is_not_tracked(monkeypatch: pytest.MonkeyPatch) -> None:
    _tracker, _clock = install_provider_health(monkeypatch, threshold=1)
    untracked: Any = SimpleNamespace(id="not-a-uuid", provider="openai", model="m")
    assert model_key(untracked) is None
    for _ in range(3):
        with pytest.raises(LLMHTTPError):
            _ = await guarded(untracked, _fails_with(LLMHTTPError(503, "busy")))
    assert not provider_health.is_unavailable(untracked)


@pytest.fixture
def fresh_health() -> Iterator[None]:
    provider_health.reset_provider_health()
    yield
    provider_health.reset_provider_health()


def test_provider_health_settings_default_to_a_handful_of_failures_and_a_short_cooldown() -> None:
    settings = Settings()
    assert (settings.LLM_PROVIDER_FAILURE_THRESHOLD, settings.LLM_PROVIDER_COOLDOWN_SECONDS) == (5, 30.0)


@pytest.mark.parametrize(
    "overrides",
    [
        {"LLM_PROVIDER_FAILURE_THRESHOLD": 0},
        {"LLM_PROVIDER_COOLDOWN_SECONDS": 0},
        {"LLM_PROVIDER_COOLDOWN_SECONDS": float("nan")},
        {"LLM_PROVIDER_COOLDOWN_SECONDS": float("inf")},
    ],
)
def test_provider_health_settings_reject_values_that_would_disable_or_wedge_the_circuit(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(pydantic.ValidationError):
        _ = Settings.model_validate(overrides)


@pytest.mark.usefixtures("fresh_health")
def test_the_process_tracker_is_sized_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    configured = Settings(LLM_PROVIDER_FAILURE_THRESHOLD=2, LLM_PROVIDER_COOLDOWN_SECONDS=12.0)
    monkeypatch.setattr(provider_health, "get_settings", lambda: configured)
    tracker = provider_health.health_tracker()
    assert tracker is provider_health.health_tracker()
    assert [tracker.failed(KEY, Admission.CALL) for _ in range(2)] == [False, True]
    assert 0 < tracker.retry_in(KEY) <= 12.0
    provider_health.reset_provider_health()
    assert provider_health.health_tracker() is not tracker
