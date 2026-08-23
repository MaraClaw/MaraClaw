"""Small LRU of constructed LLM clients keyed without storing raw secrets."""

from __future__ import annotations

import hashlib
from typing import Final

from app.services.llm.base import LLMClient
from app.services.llm.http_pool import reset_http_pool

_MAX_CLIENTS: Final = 64
_CACHE: dict[tuple[object, ...], LLMClient] = {}


def fingerprint_secret(value: str) -> str:
    """Hash a key or token for a cache key. Empty stays empty."""
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def cache_get(key: tuple[object, ...]) -> LLMClient | None:
    client = _CACHE.get(key)
    if client is None:
        return None
    _ = _CACHE.pop(key, None)
    _CACHE[key] = client
    return client


def cache_put(key: tuple[object, ...], client: LLMClient) -> LLMClient:
    _ = _CACHE.pop(key, None)
    _CACHE[key] = client
    while len(_CACHE) > _MAX_CLIENTS:
        oldest = next(iter(_CACHE), None)
        if oldest is None:
            break
        _ = _CACHE.pop(oldest, None)
    return client


def reset_llm_client_cache() -> None:
    """Drop cached wrappers and shared HTTP clients. Tests call this."""
    _CACHE.clear()
    reset_http_pool()
