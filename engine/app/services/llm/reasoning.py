"""Canonical reasoning-effort mapping for provider-native request fields.

Org admins pick one of ``none`` / ``low`` / ``medium`` / ``high`` / ``xhigh``.
Each provider gets a different request shape (and some clamp levels they do
not expose). This module is the only place those translations live.
"""

from __future__ import annotations

from typing import Any, Literal

from app.services.llm.registry import normalize_provider

ReasoningEffort = Literal["none", "low", "medium", "high", "xhigh"]

CANONICAL_REASONING_EFFORTS: tuple[ReasoningEffort, ...] = ("none", "low", "medium", "high", "xhigh")

# What the Models UI may offer. Values missing from a provider are still
# accepted and clamped in ``native_reasoning_effort``.
_PROVIDER_EFFORTS: dict[str, tuple[ReasoningEffort, ...]] = {
    "openai": CANONICAL_REASONING_EFFORTS,
    "openai-response": CANONICAL_REASONING_EFFORTS,
    "azure": CANONICAL_REASONING_EFFORTS,
    "grok": CANONICAL_REASONING_EFFORTS,
    "openrouter": CANONICAL_REASONING_EFFORTS,
    "anthropic": CANONICAL_REASONING_EFFORTS,
    "gemini": ("none", "low", "medium", "high"),
    "deepseek": CANONICAL_REASONING_EFFORTS,
    "qwen": CANONICAL_REASONING_EFFORTS,
}

# Native value sent on the wire after clamping.
_NATIVE_EFFORT: dict[str, dict[ReasoningEffort, str]] = {
    "gemini": {"none": "minimal", "low": "low", "medium": "medium", "high": "high", "xhigh": "high"},
    "deepseek": {"none": "none", "low": "low", "medium": "high", "high": "high", "xhigh": "max"},
    "qwen": {"none": "none", "low": "low", "medium": "medium", "high": "xhigh", "xhigh": "xhigh"},
    "grok": {"none": "low", "low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh"},
}


def normalize_reasoning_effort(value: object) -> ReasoningEffort | None:
    """Return a canonical effort, or None when unset / unknown."""
    if value is None:
        return None
    text = str(value).strip().lower()
    for effort in CANONICAL_REASONING_EFFORTS:
        if text == effort:
            return effort
    return None


def supported_reasoning_efforts(provider: str) -> tuple[str, ...]:
    """Efforts the admin UI should offer for ``provider``."""
    return _PROVIDER_EFFORTS.get(normalize_provider(provider), ())


def native_reasoning_effort(provider: str, effort: ReasoningEffort) -> str:
    """Map a canonical effort onto the provider's native enum value."""
    key = normalize_provider(provider)
    table = _NATIVE_EFFORT.get(key)
    if table:
        return table[effort]
    return effort


def reasoning_effort_fields(provider: str, effort: object) -> dict[str, Any]:
    """Return extra request fields for ``provider`` at ``effort``.

    Empty when the provider has no effort control or ``effort`` is unset.
    """
    normalized = normalize_reasoning_effort(effort)
    if normalized is None:
        return {}
    key = normalize_provider(provider)
    if key not in _PROVIDER_EFFORTS:
        return {}
    native = native_reasoning_effort(key, normalized)

    if key == "anthropic":
        if normalized == "none":
            return {"thinking": {"type": "disabled"}, "output_config": {"effort": "low"}}
        return {"output_config": {"effort": native}}
    if key == "openai-response":
        return {"reasoning": {"effort": native}}
    if key == "gemini":
        return {"generationConfig": {"thinkingConfig": {"thinkingLevel": native}}}
    if key == "deepseek":
        if normalized == "none":
            return {"thinking": {"type": "disabled"}}
        return {"reasoning_effort": native, "thinking": {"type": "enabled"}}
    if key == "qwen":
        if normalized == "none":
            return {"enable_thinking": False}
        return {"reasoning_effort": native, "enable_thinking": True}
    # OpenAI chat, Azure, Grok, OpenRouter: top-level reasoning_effort.
    return {"reasoning_effort": native}


def apply_reasoning_effort(
    payload: dict[str, Any],
    *,
    provider: str,
    effort: object,
) -> dict[str, Any]:
    """Merge mapped effort fields into ``payload`` and return it."""
    extra = reasoning_effort_fields(provider, effort)
    if not extra:
        return payload
    _deep_merge(payload, extra)
    return payload


def _deep_merge(target: dict[str, Any], extra: dict[str, Any]) -> None:
    for key, value in extra.items():
        existing = target.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            _deep_merge(existing, value)
        else:
            target[key] = value
