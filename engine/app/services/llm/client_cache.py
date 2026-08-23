"""Small LRU of constructed LLM clients keyed without storing raw secrets."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Final

from app.services.llm.base import LLMClient
from app.services.llm.http_pool import reset_http_pool

_MAX_CLIENTS: Final = 64
_TTL_SECONDS: Final = 600.0


@dataclass(slots=True)
class _CacheEntry:
    client: LLMClient
    created_at: float


_CACHE: dict[tuple[object, ...], _CacheEntry] = {}


def fingerprint_secret(value: str) -> str:
    """Hash a key or token for a cache key. Empty stays empty."""
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _wipe_client(client: LLMClient) -> None:
    client.api_key = ""
    if hasattr(client, "_client"):
        client._client = None  # type: ignore[attr-defined]


def cache_get(key: tuple[object, ...]) -> LLMClient | None:
    entry = _CACHE.get(key)
    if entry is None:
        return None
    if time.monotonic() - entry.created_at > _TTL_SECONDS:
        _ = _CACHE.pop(key, None)
        _wipe_client(entry.client)
        return None
    _ = _CACHE.pop(key, None)
    _CACHE[key] = entry
    return entry.client


def cache_put(key: tuple[object, ...], client: LLMClient) -> LLMClient:
    _ = _CACHE.pop(key, None)
    _CACHE[key] = _CacheEntry(client=client, created_at=time.monotonic())
    while len(_CACHE) > _MAX_CLIENTS:
        oldest = next(iter(_CACHE), None)
        if oldest is None:
            break
        evicted = _CACHE.pop(oldest, None)
        if evicted is not None:
            _wipe_client(evicted.client)
    return client


def _wipe_all_wrappers() -> None:
    for entry in _CACHE.values():
        _wipe_client(entry.client)
    _CACHE.clear()


def reset_llm_client_cache() -> None:
    """Drop cached wrappers and shared HTTP clients. Tests call this."""
    _wipe_all_wrappers()
    reset_http_pool()


async def shutdown_llm_clients() -> None:
    """Wipe wrappers and aclose pooled transports. App lifespan calls this."""
    _wipe_all_wrappers()
    from app.services.llm.http_pool import aclose_http_pool

    await aclose_http_pool()
