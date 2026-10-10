"""Registered providers are the factory's authorization boundary."""

from collections.abc import Iterator
from typing import Never

import pytest

from app.services.llm import factory
from app.services.llm.base import LLMClient, LLMError
from app.services.llm.client_cache import cache_put, fingerprint_secret, reset_llm_client_cache
from app.services.llm.providers import OpenAICompatibleClient
from app.services.llm.registry import PROVIDER_ALIASES, PROVIDER_CLIENTS, PROVIDER_REGISTRY


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
