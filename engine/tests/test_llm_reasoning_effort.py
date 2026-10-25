"""Canonical reasoning-effort mapping for provider-native payloads."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.json_types import JsonObject
from app.schemas.schemas import LLMModelCreate, LLMModelUpdate
from app.services.llm.providers.anthropic import AnthropicClient
from app.services.llm.providers.gemini import GeminiClient
from app.services.llm.providers.openai_compatible import OpenAICompatibleClient
from app.services.llm.providers.openai_responses import OpenAIResponsesClient
from app.services.llm.reasoning import (
    apply_reasoning_effort,
    native_reasoning_effort,
    normalize_reasoning_effort,
    reasoning_effort_fields,
    supported_reasoning_efforts,
)


def test_normalize_accepts_canonical_and_rejects_unknown():
    assert normalize_reasoning_effort("HIGH") == "high"
    assert normalize_reasoning_effort("xhigh") == "xhigh"
    assert normalize_reasoning_effort("none") == "none"
    assert normalize_reasoning_effort("unknown") is None
    assert normalize_reasoning_effort("") is None
    assert normalize_reasoning_effort(None) is None


@pytest.mark.parametrize(
    ("value", "expected"), [("MAX", "max"), (" max ", "max"), ("ULTRA", "ultra"), (" ultra ", "ultra")]
)
def test_normalize_accepts_highest_presets(value: str, expected: str) -> None:
    # Given a configured highest preset with casing or whitespace.
    # When the runtime normalizes it.
    result = normalize_reasoning_effort(value)
    # Then the canonical preset survives normalization.
    assert result == expected


@pytest.mark.parametrize("schema", [LLMModelCreate, LLMModelUpdate])
@pytest.mark.parametrize("effort", [None, "none", "low", "medium", "high", "xhigh", "max", "ultra"])
def test_model_schemas_preserve_canonical_effort(
    schema: type[LLMModelCreate | LLMModelUpdate], effort: str | None
) -> None:
    # Given an API request for a model's reasoning preset.
    request = {
        "provider": "openai",
        "model": "test-model",
        "api_key": "secret",
        "label": "Test",
        "reasoning_effort": effort,
    }
    # When either write schema parses the request.
    model = schema.model_validate(request)
    # Then the accepted value is retained for persistence, not replaced by its native ceiling.
    assert model.reasoning_effort == effort
    assert model.model_dump(exclude_unset=True)["reasoning_effort"] == effort


@pytest.mark.parametrize("schema", [LLMModelCreate, LLMModelUpdate])
@pytest.mark.parametrize("effort", ["", "maximum", "ultra+", "MAX", " ultra "])
def test_model_schemas_reject_noncanonical_effort(schema: type[LLMModelCreate | LLMModelUpdate], effort: str) -> None:
    # Given a noncanonical API value (runtime normalization does not loosen API validation).
    request = {
        "provider": "openai",
        "model": "test-model",
        "api_key": "secret",
        "label": "Test",
        "reasoning_effort": effort,
    }
    # When either write schema parses it, then validation rejects the reasoning field.
    with pytest.raises(ValidationError) as error:
        schema.model_validate(request)
    assert error.value.errors()[0]["loc"] == ("reasoning_effort",)


def test_supported_efforts_per_provider():
    assert supported_reasoning_efforts("grok") == ("none", "low", "medium", "high", "xhigh")
    assert supported_reasoning_efforts("xai") == ("none", "low", "medium", "high", "xhigh")
    assert supported_reasoning_efforts("qwen") == ("none", "low", "medium", "high", "xhigh")
    assert supported_reasoning_efforts("gemini") == ("none", "low", "medium", "high")
    assert supported_reasoning_efforts("ollama") == ()


def test_native_clamps_for_gemini_deepseek_qwen():
    assert native_reasoning_effort("gemini", "xhigh") == "high"
    assert native_reasoning_effort("gemini", "none") == "minimal"
    assert native_reasoning_effort("deepseek", "medium") == "high"
    assert native_reasoning_effort("deepseek", "xhigh") == "max"
    assert native_reasoning_effort("qwen", "high") == "xhigh"
    assert native_reasoning_effort("grok", "xhigh") == "xhigh"
    assert native_reasoning_effort("grok", "none") == "low"
    assert native_reasoning_effort("openai", "low") == "low"
    assert native_reasoning_effort("openai", "none") == "none"


def test_payload_shapes():
    assert reasoning_effort_fields("grok", "high") == {"reasoning_effort": "high"}
    assert reasoning_effort_fields("grok", "none") == {"reasoning_effort": "low"}
    assert reasoning_effort_fields("openai", "none") == {"reasoning_effort": "none"}
    assert reasoning_effort_fields("openai-response", "none") == {"reasoning": {"effort": "none"}}
    assert reasoning_effort_fields("openai-response", "xhigh") == {"reasoning": {"effort": "xhigh"}}
    assert reasoning_effort_fields("anthropic", "medium") == {"output_config": {"effort": "medium"}}
    assert reasoning_effort_fields("anthropic", "none") == {
        "thinking": {"type": "disabled"},
        "output_config": {"effort": "low"},
    }
    assert reasoning_effort_fields("gemini", "xhigh") == {
        "generationConfig": {"thinkingConfig": {"thinkingLevel": "high"}}
    }
    assert reasoning_effort_fields("gemini", "none") == {
        "generationConfig": {"thinkingConfig": {"thinkingLevel": "minimal"}}
    }
    assert reasoning_effort_fields("deepseek", "xhigh") == {
        "reasoning_effort": "max",
        "thinking": {"type": "enabled"},
    }
    assert reasoning_effort_fields("deepseek", "none") == {"thinking": {"type": "disabled"}}
    assert reasoning_effort_fields("qwen", "high") == {
        "reasoning_effort": "xhigh",
        "enable_thinking": True,
    }
    assert reasoning_effort_fields("qwen", "none") == {"enable_thinking": False}
    assert reasoning_effort_fields("ollama", "high") == {}
    assert reasoning_effort_fields("grok", None) == {}


def test_apply_merges_nested_gemini_config():
    config: dict[str, object] = {"temperature": 0.2, "maxOutputTokens": 1024}
    payload: dict[str, object] = {"generationConfig": config}
    apply_reasoning_effort(payload, provider="gemini", effort="low")
    assert config["temperature"] == 0.2
    assert config["thinkingConfig"] == {"thinkingLevel": "low"}


def test_apply_preserves_arbitrary_nested_options_when_merging_effort():
    option = object()
    config: dict[str, object] = {"custom_option": option, "effort": "low"}
    payload: dict[str, object] = {"output_config": config}

    result = apply_reasoning_effort(payload, provider="anthropic", effort="high")

    assert result is payload
    assert payload["output_config"] is config
    assert config["custom_option"] is option
    assert config["effort"] == "high"


def test_anthropic_preserves_arbitrary_kwargs_when_applying_reasoning():
    client = AnthropicClient("secret", model="claude")
    option = object()

    payload = client._build_payload([], None, None, None, reasoning_effort="high", custom_option=option)

    assert payload["custom_option"] is option
    assert payload["output_config"] == {"effort": "high"}
    assert "reasoning_effort" not in payload


@pytest.mark.parametrize("effort", ["max", "ultra"])
@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("openai", {"reasoning_effort": "max"}),
        ("azure", {"reasoning_effort": "max"}),
        ("openrouter", {"reasoning_effort": "max"}),
        ("deepseek", {"reasoning_effort": "max", "thinking": {"type": "enabled"}}),
        ("grok", {"reasoning_effort": "xhigh"}),
        ("xai", {"reasoning_effort": "xhigh"}),
        ("qwen", {"reasoning_effort": "xhigh", "enable_thinking": True}),
    ],
)
def test_compatible_payload_uses_provider_ceiling(provider: str, effort: str, expected: JsonObject) -> None:
    # Given an actual compatible client and a highest preset, including unoffered presets.
    client = OpenAICompatibleClient("secret", model="test-model")
    # When building the provider request used by complete/stream.
    payload = client._build_payload([], None, None, None, llm_provider=provider, reasoning_effort=effort)
    # Then the wire fields contain only the provider's native ceiling.
    assert {key: payload[key] for key in expected} == expected
    assert "llm_provider" not in payload


@pytest.mark.parametrize("effort", ["max", "ultra"])
@pytest.mark.parametrize(
    ("client_type", "expected"),
    [
        (AnthropicClient, {"output_config": {"effort": "max"}}),
        (OpenAIResponsesClient, {"reasoning": {"effort": "max"}}),
        (GeminiClient, {"generationConfig": {"thinkingConfig": {"thinkingLevel": "high"}}}),
    ],
)
def test_native_payload_uses_provider_ceiling(
    client_type: type[AnthropicClient | OpenAIResponsesClient | GeminiClient],
    effort: str,
    expected: JsonObject,
) -> None:
    # Given an actual native-protocol client and a highest preset.
    client = client_type("secret", model="test-model")
    # When building the provider request used by complete/stream.
    payload = client._build_payload([], None, None, None, reasoning_effort=effort)
    # Then the effort is nested correctly with no raw top-level passthrough.
    assert {key: payload[key] for key in expected} == expected
    assert "reasoning_effort" not in payload
