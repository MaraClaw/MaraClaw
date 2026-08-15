"""Shared xAI key and base-url resolution for Imagine and X Search."""

from __future__ import annotations

from urllib.parse import urlparse

DEFAULT_XAI_BASE_URL = "https://api.x.ai/v1"
ALLOWED_XAI_HOSTS = frozenset({"api.x.ai"})


def resolve_xai_api_key(config_api_key: str) -> str:
    key = config_api_key.strip()
    if key:
        return key
    from app.config import get_settings

    return get_settings().XAI_API_KEY.strip()


def resolve_xai_base_url(config_base_url: str) -> tuple[str, str | None]:
    """Return `(url, error)`. Only `api.x.ai` is accepted as a custom host."""
    raw = config_base_url.strip()
    if not raw:
        return DEFAULT_XAI_BASE_URL, None
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in ALLOWED_XAI_HOSTS:
        return DEFAULT_XAI_BASE_URL, "xAI base_url must be https://api.x.ai/v1"
    return raw.rstrip("/"), None


def missing_xai_key_message(tool_label: str) -> str:
    return (
        f"❌ {tool_label} API key not configured. "
        + "Set XAI_API_KEY or PUT /api/tools/{id} with config.api_key for this tool."
    )
