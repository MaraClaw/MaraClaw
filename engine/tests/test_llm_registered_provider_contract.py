"""Registered providers are the factory's authorization boundary and share one retry policy."""

import asyncio
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any, Never
from uuid import uuid4

import httpx
import pytest
from tests.llm_retry_fakes import (
    ANTHROPIC,
    API_KEY,
    CALLS,
    FAILURES,
    FIXED_NOW,
    OPENAI_COMPATIBLE,
    PROMPT,
    Mode,
    Outcome,
    ProviderCase,
    ScriptedHttpClient,
    ScriptedResponse,
    call_provider,
    capture_retry_logs,
    install_retry_policy,
)

from app.services.llm import caller, factory
from app.services.llm.base import LLMClient, LLMError
from app.services.llm.client_cache import cache_put, fingerprint_secret, reset_llm_client_cache
from app.services.llm.failover import FailoverErrorType, classify_error
from app.services.llm.providers import GeminiClient, OpenAICompatibleClient
from app.services.llm.registry import PROVIDER_ALIASES, PROVIDER_CLIENTS, PROVIDER_REGISTRY
from app.services.llm.retry_policy import LLMHTTPError, RetryPolicy, call_with_retry
from app.services.llm.types import LLMMessage


@pytest.fixture(autouse=True)
def isolated_client_cache() -> Iterator[None]:
    reset_llm_client_cache()
    yield
    reset_llm_client_cache()


@pytest.mark.parametrize("provider", tuple(PROVIDER_REGISTRY))
@pytest.mark.parametrize("endpoint", [None, "https://gateway.example.invalid/v42"])
def test_registered_provider_preserves_protocol_and_endpoint(provider: str, endpoint: str | None) -> None:
    # Given a registered provider and an optional explicit endpoint.
    spec = PROVIDER_REGISTRY[provider]
    expected_endpoint = endpoint or spec.default_base_url or "https://api.openai.com/v1"
    # When the factory constructs its real, lazy protocol wrapper.
    client = factory.create_llm_client(provider, "test-key", "test-model", endpoint, timeout=7)
    # Then registration retains its protocol, endpoint, and request configuration.
    assert type(client) is PROVIDER_CLIENTS[provider]
    assert client.base_url == expected_endpoint
    assert client.model == "test-model"
    assert client.api_key == "test-key"
    assert client.timeout == 7
    if isinstance(client, OpenAICompatibleClient):
        assert client.supports_tool_choice is spec.supports_tool_choice
        assert client.supports_cache_control is (provider == "qwen")


@pytest.mark.parametrize("provider", tuple(PROVIDER_REGISTRY))
def test_registered_provider_reuses_identical_wrapper(provider: str) -> None:
    # Given a previously constructed registered wrapper.
    first = factory.create_llm_client(provider, "test-key", "test-model", "https://gateway.example.invalid/v42")
    # When the same normalized config is requested.
    reused = factory.create_llm_client(
        f" {provider.upper()} ", "test-key", "test-model", "https://gateway.example.invalid/v42"
    )
    # Then the cache still reuses the wrapper.
    assert reused is first


@pytest.mark.parametrize(("alias", "provider"), tuple(PROVIDER_ALIASES.items()))
def test_existing_alias_reuses_registered_provider_wrapper(alias: str, provider: str) -> None:
    # Given the canonical provider's cached wrapper.
    canonical = factory.create_llm_client(provider, "test-key", "test-model")
    # When a current alias is normalized.
    aliased = factory.create_llm_client(f" {alias.upper()} ", "test-key", "test-model")
    # Then it selects the same registered protocol and cache entry.
    assert aliased is canonical


@pytest.mark.parametrize("provider", ["__no_such_provider__", "", "  ", "openai-typo", "anthropic-typo"])
@pytest.mark.parametrize("endpoint", [None, "https://gateway.example.invalid/v42"])
def test_unregistered_provider_rejected_even_with_explicit_endpoint(provider: str, endpoint: str | None) -> None:
    # Given a name that has no registry entry, regardless of its endpoint.
    # When it reaches the factory, then no protocol wrapper is authorized.
    with pytest.raises(LLMError):
        factory.create_llm_client(provider, "test-key", "test-model", endpoint)


def test_unregistered_provider_rejected_before_cache_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a cache seam that must never be reached for an unregistered name.
    def forbidden_lookup(key: tuple[object, ...]) -> LLMClient | None:
        pytest.fail("unregistered provider reached cache lookup")

    monkeypatch.setattr(factory, "cache_get", forbidden_lookup)
    # When it reaches the factory, then registration fails first.
    with pytest.raises(LLMError):
        factory.create_llm_client("__no_such_provider__", "test-key", "test-model")


def test_unregistered_provider_rejected_despite_preexisting_cache_entry() -> None:
    # Given a wrapper cached under the retired fallback's exact config.
    provider = "__no_such_provider__"
    endpoint = "https://gateway.example.invalid/v42"
    retired = OpenAICompatibleClient(api_key="test-key", model="test-model", base_url=endpoint)
    cache_put(("factory", provider, "test-model", endpoint, 120.0, fingerprint_secret("test-key")), retired)
    # When the unknown provider is requested, then stale cache cannot authorize it.
    with pytest.raises(LLMError):
        factory.create_llm_client(provider, "test-key", "test-model", endpoint)


def test_unregistered_provider_rejected_before_client_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given constructor seams that must never be reached for an unknown name.
    def forbidden_constructor(**kwargs: str | float | bool | None) -> Never:
        pytest.fail("unregistered provider reached client construction")

    for constructor in ("AnthropicClient", "GeminiClient", "OpenAICompatibleClient", "OpenAIResponsesClient"):
        monkeypatch.setattr(factory, constructor, forbidden_constructor)
    # When it reaches the factory, then registration fails before construction.
    with pytest.raises(LLMError):
        factory.create_llm_client("__no_such_provider__", "test-key", "test-model")


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    return install_retry_policy(monkeypatch)


@pytest.mark.parametrize(("case", "mode"), CALLS)
@pytest.mark.parametrize("failure", FAILURES)
async def test_transient_failure_is_retried_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    case: ProviderCase,
    mode: Mode,
    failure: Callable[[], Outcome],
    sleeps: list[float],
) -> None:
    # Given a transient first failure and a healthy second answer.
    failed = failure()
    http = ScriptedHttpClient(failed, case.ok())
    # When the provider is called.
    response = await call_provider(monkeypatch, case, mode, http)
    # Then the second request answers after one jittered wait and the failed response was closed.
    assert response.content == "Hello"
    assert len(http.requests) == 2
    assert sleeps == [0.75]
    if isinstance(failed, ScriptedResponse):
        assert failed.closed


@pytest.mark.parametrize(("case", "mode"), CALLS)
@pytest.mark.parametrize(("header", "waited"), [("7", 7.0), ("3600", 30.0)])
async def test_retry_after_sets_the_wait_and_is_capped(
    monkeypatch: pytest.MonkeyPatch, case: ProviderCase, mode: Mode, header: str, waited: float, sleeps: list[float]
) -> None:
    # Given a 429 that names its own wait, mistaken or hostile values included.
    http = ScriptedHttpClient(ScriptedResponse(429, body="slow down", headers={"retry-after": header}), case.ok())
    # When the provider is called.
    response = await call_provider(monkeypatch, case, mode, http)
    # Then the wait is the header's value, never above the cap.
    assert response.content == "Hello"
    assert sleeps == [waited]


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("7", 7.0),
        (" 2.5 ", 2.5),
        ("0", 0.0),
        ("Fri, 20 Nov 2026 12:00:10 GMT", 10.0),
        ("3600", 30.0),
        ("Fri, 20 Nov 2026 14:00:00 GMT", 30.0),
        ("Fri, 20 Nov 2026 11:59:00 GMT", 0.75),
        ("soon", 0.75),
        ("", 0.75),
        ("-5", 0.75),
        ("nan", 0.75),
        ("inf", 0.75),
        (None, 0.75),
    ],
)
def test_retry_after_forms_are_parsed_and_garbage_falls_back_to_backoff(header: str | None, expected: float) -> None:
    # Given a policy with a fixed clock and mid-range jitter.
    policy = RetryPolicy(jitter=lambda: 0.5, now=lambda: FIXED_NOW)
    # When the wait after a first failure is computed.
    # Then seconds and HTTP dates set it (capped) and anything unusable yields the jittered backoff.
    assert policy.delay_for(1, header) == expected


@pytest.mark.parametrize(
    ("attempt", "jitter", "expected"), [(1, 0.0, 0.5), (1, 1.0, 1.0), (2, 0.0, 1.0), (2, 1.0, 2.0), (5, 1.0, 8.0)]
)
def test_backoff_without_retry_after_is_jittered_exponential_and_capped(
    attempt: int, jitter: float, expected: float
) -> None:
    assert RetryPolicy(jitter=lambda: jitter).delay_for(attempt, None) == expected


@pytest.mark.parametrize(("case", "mode"), CALLS)
@pytest.mark.parametrize("status", [400, 401, 403, 422])
async def test_client_errors_are_raised_on_the_first_attempt(
    monkeypatch: pytest.MonkeyPatch, case: ProviderCase, mode: Mode, status: int, sleeps: list[float]
) -> None:
    # Given a provider that rejects the request.
    http = ScriptedHttpClient(ScriptedResponse(status, body="rejected"), case.ok())
    # When the provider is called.
    with pytest.raises(LLMError, match=f"^HTTP {status}") as raised:
        await call_provider(monkeypatch, case, mode, http)
    # Then nothing is retried and the failover does not spend the fallback model on it.
    assert len(http.requests) == 1
    assert sleeps == []
    assert not caller.is_retryable_error(f"[LLM Error] {raised.value}")


@pytest.mark.parametrize(("case", "mode"), CALLS)
@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (lambda: ScriptedResponse(429, body="rate limit hit"), "HTTP 429: rate limit hit"),
        (lambda: ScriptedResponse(503, body="upstream busy"), "HTTP 503: upstream busy"),
        (lambda: httpx.ConnectError("boom"), "Connection failed after 3 attempts: boom"),
    ],
    ids=["429", "503", "connect-error"],
)
async def test_exhausted_budget_raises_an_error_the_failover_classifies_as_retryable(
    monkeypatch: pytest.MonkeyPatch,
    case: ProviderCase,
    mode: Mode,
    failure: Callable[[], Outcome],
    message: str,
    sleeps: list[float],
) -> None:
    # Given a provider that never recovers.
    http = ScriptedHttpClient(failure(), failure(), failure())
    # When the provider is called.
    with pytest.raises(LLMError) as raised:
        await call_provider(monkeypatch, case, mode, http)
    # Then the budget of three requests is spent with doubling jittered waits and the error keeps its shape.
    assert str(raised.value) == message
    assert len(http.requests) == 3
    assert sleeps == [0.75, 1.5]
    assert classify_error(raised.value) is FailoverErrorType.RETRYABLE
    assert caller.is_retryable_error(f"[LLM Error] {raised.value}")


@pytest.mark.parametrize(("case", "mode"), CALLS)
async def test_cancellation_during_the_wait_makes_no_further_request(
    monkeypatch: pytest.MonkeyPatch, case: ProviderCase, mode: Mode
) -> None:
    # Given a retry wait that parks until the caller is cancelled.
    parked = asyncio.Event()

    async def park(_seconds: float) -> None:
        parked.set()
        await asyncio.Event().wait()

    sleeps = install_retry_policy(monkeypatch, sleep=park)
    failed = ScriptedResponse(503, body="upstream busy")
    http = ScriptedHttpClient(failed, case.ok())
    task = asyncio.create_task(call_provider(monkeypatch, case, mode, http))
    async with asyncio.timeout(5):
        await parked.wait()
    # When the caller is cancelled mid-wait.
    task.cancel()
    # Then the cancellation propagates and no second request is made.
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(http.requests) == 1
    assert sleeps == [0.75]
    assert failed.closed


@pytest.mark.parametrize(("case", "mode"), CALLS)
async def test_retry_logs_carry_no_secret_header_or_prompt(
    monkeypatch: pytest.MonkeyPatch, case: ProviderCase, mode: Mode, sleeps: list[float]
) -> None:
    # Given two retried failures, one of them naming a wait.
    logged = capture_retry_logs(monkeypatch)
    http = ScriptedHttpClient(
        ScriptedResponse(429, body=PROMPT, headers={"retry-after": "2"}), httpx.ReadError("dropped"), case.ok()
    )
    # When the provider is called.
    await call_provider(monkeypatch, case, mode, http)
    # Then each log names the provider, the status or error, the attempt and the delay, and nothing sensitive.
    assert API_KEY in str(http.requests[0]["kwargs"]["headers"])
    assert len(logged) == 2
    assert [case.id, "HTTP 429", "1/3", "2.00"] == [
        token for token in (case.id, "HTTP 429", "1/3", "2.00") if token in logged[0]
    ]
    assert [case.id, "ReadError", "2/3", "1.50"] == [
        token for token in (case.id, "ReadError", "2/3", "1.50") if token in logged[1]
    ]
    assert not any(API_KEY in line or PROMPT in line for line in logged)


@pytest.mark.parametrize(
    "error",
    [pytest.param(LLMHTTPError(status, "busy"), id=str(status)) for status in (429, 500, 502, 503, 504, 529)]
    + [
        pytest.param(httpx.ConnectError("x"), id="ConnectError"),
        pytest.param(httpx.ReadError("x"), id="ReadError"),
        pytest.param(httpx.ConnectTimeout("x"), id="ConnectTimeout"),
    ],
)
async def test_transient_errors_are_retried(error: Exception, sleeps: list[float]) -> None:
    attempts: list[int] = []

    async def attempt() -> str:
        attempts.append(1)
        if len(attempts) == 1:
            raise error
        return "done"

    assert await call_with_retry(attempt, provider="test") == "done"
    assert len(attempts) == 2
    assert sleeps == [0.75]


@pytest.mark.parametrize(
    "error",
    [LLMHTTPError(404, "missing"), LLMHTTPError(501, "unsupported"), httpx.ReadTimeout("slow"), ValueError("bad")],
    ids=["404", "501", "ReadTimeout", "ValueError"],
)
async def test_other_failures_propagate_on_the_first_attempt(error: Exception, sleeps: list[float]) -> None:
    attempts: list[int] = []

    async def attempt() -> str:
        attempts.append(1)
        raise error

    with pytest.raises(type(error)):
        await call_with_retry(attempt, provider="test")
    assert len(attempts) == 1
    assert sleeps == []


async def test_gemini_openai_compatible_base_retries_through_its_fallback_client(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    # Given a Gemini row on a legacy /openai gateway, which delegates to the OpenAI-compatible client.
    client = GeminiClient(API_KEY, base_url="https://gateway.example.invalid/openai", model="gemini-test")
    fallback = await client._get_openai_fallback_client()
    http = ScriptedHttpClient(ScriptedResponse(503, body="upstream busy"), OPENAI_COMPATIBLE.ok())

    async def get_client() -> ScriptedHttpClient:
        return http

    monkeypatch.setattr(fallback, "_get_client", get_client)
    # When it completes.
    response = await client.complete(messages=[LLMMessage(role="user", content=PROMPT)])
    # Then the transient failure was retried once.
    assert response.content == "Hello"
    assert len(http.requests) == 2
    assert sleeps == [0.75]


@pytest.mark.parametrize("fallback_recovers", [True, False])
async def test_failover_falls_back_exactly_once_after_the_retry_budget_is_spent(
    monkeypatch: pytest.MonkeyPatch, fallback_recovers: bool, sleeps: list[float]
) -> None:
    # Given a primary provider that keeps answering 503, and a fallback that is healthy or just as unwell.
    primary: Any = SimpleNamespace(provider="anthropic", model="primary")
    fallback: Any = SimpleNamespace(provider="anthropic", model="fallback")
    unwell = [ScriptedResponse(503, body="upstream busy") for _ in range(3)]
    http_by_model = {
        "primary": ScriptedHttpClient(*unwell),
        "fallback": ScriptedHttpClient(
            ANTHROPIC.ok() if fallback_recovers else ScriptedResponse(503, body="upstream busy"),
            *([] if fallback_recovers else [ScriptedResponse(503, body="upstream busy") for _ in range(2)]),
        ),
    }
    calls: list[str] = []

    async def call_llm_over_real_client(model: SimpleNamespace, *_args: object, **_kwargs: object) -> str:
        # Mirrors caller.call_llm: a provider LLMError becomes the "[LLM Error] ..." reply the failover reads.
        calls.append(model.model)
        client = ANTHROPIC.make()

        async def get_client() -> ScriptedHttpClient:
            return http_by_model[model.model]

        monkeypatch.setattr(client, "_get_client", get_client)
        try:
            response = await client.stream(messages=[LLMMessage(role="user", content=PROMPT)])
        except LLMError as error:
            return f"[LLM Error] {error}"
        return response.content

    monkeypatch.setattr(caller, "call_llm", call_llm_over_real_client)
    # When the failover wrapper runs.
    result = await caller.call_llm_with_failover(primary, fallback, [], "Agent", "role", agent_id=uuid4())
    # Then the primary spent its budget, the fallback was tried exactly once, and nothing else was.
    assert calls == ["primary", "fallback"]
    assert len(http_by_model["primary"].requests) == 3
    assert len(http_by_model["fallback"].requests) == (1 if fallback_recovers else 3)
    assert (result == "Hello") if fallback_recovers else result.startswith("⚠️ Model call failed")
