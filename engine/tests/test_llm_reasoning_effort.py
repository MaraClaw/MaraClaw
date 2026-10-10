"""Canonical reasoning-effort mapping for provider-native payloads."""

from __future__ import annotations

from app.services.llm.providers.anthropic import AnthropicClient
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
    assert normalize_reasoning_effort("max") is None
    assert normalize_reasoning_effort("") is None
    assert normalize_reasoning_effort(None) is None


def test_supported_efforts_per_provider():
    assert supported_reasoning_efforts("grok") == ("none", "low", "medium", "high", "xhigh")
    assert supported_reasoning_efforts("xai") == ("none", "low", "medium", "high", "xhigh")
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
