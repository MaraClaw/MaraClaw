import pytest

from app.core.json_types import JsonObject, is_json_object
from app.services.llm.providers import (
    AnthropicClient,
    GeminiClient,
    OpenAICompatibleClient,
    OpenAIResponsesClient,
)

type PayloadClient = AnthropicClient | GeminiClient | OpenAICompatibleClient | OpenAIResponsesClient


@pytest.mark.parametrize("temperature", [None, 0, 0.2])
@pytest.mark.parametrize(
    ("client_type", "config_key"),
    [
        (AnthropicClient, None),
        (GeminiClient, "generationConfig"),
        (OpenAICompatibleClient, None),
        (OpenAIResponsesClient, None),
    ],
)
def test_payload_temperature_when_optional(
    client_type: type[PayloadClient], config_key: str | None, temperature: float | None
) -> None:
    # Given
    client = client_type("secret", model="test-model")

    # When
    payload: dict[str, object] = client._build_payload([], None, temperature, None)

    # Then
    config = payload if config_key is None else payload[config_key]
    assert is_json_object(config)
    if temperature is None:
        assert "temperature" not in config
    else:
        assert config["temperature"] == temperature


@pytest.mark.parametrize("temperature", [None, 0, 0.2])
def test_anthropic_temperature_when_thinking_kwargs_are_set(temperature: float | None) -> None:
    # Given
    client = AnthropicClient("secret", model="claude-test")
    thinking: JsonObject = {"type": "enabled", "budget_tokens": 1024}

    # When
    payload = client._build_payload([], None, temperature, None, thinking=thinking)

    # Then
    assert payload["thinking"] == thinking
    if temperature is None:
        assert "temperature" not in payload
    else:
        assert payload["temperature"] == temperature


@pytest.mark.parametrize("temperature", [None, 0, 0.2])
def test_gemini_temperature_when_reasoning_and_max_tokens_are_set(temperature: float | None) -> None:
    # Given
    client = GeminiClient("secret", model="gemini-test")
    expected: JsonObject = {"maxOutputTokens": 1024, "thinkingConfig": {"thinkingLevel": "high"}}
    if temperature is not None:
        expected["temperature"] = temperature

    # When
    payload: dict[str, object] = client._build_payload([], None, temperature, 1024, reasoning_effort="high")

    # Then
    assert payload["generationConfig"] == expected
