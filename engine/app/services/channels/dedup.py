"""Inbound event dedupe: process-local L1 plus optional Redis SET/GET.

Prefer calling ``mark_processed`` only after a successful handling path so
retries can recover from mid-flight failures. Redis is fail-open: if it is
down, only the in-process store applies.
"""

from __future__ import annotations

from collections import OrderedDict
from threading import Lock

from app.config import get_settings
from app.core.redis_cache import cache_get, cache_key, cache_set_nx

_DEFAULT_CAP = 2000
_stores: dict[str, OrderedDict[str, None]] = {}
_lock = Lock()


def _redis_ttl() -> int:
    return int(getattr(get_settings(), "CHANNEL_DEDUP_TTL_SECONDS", 86400) or 0)


def _redis_key(namespace: str, key: str) -> str:
    return cache_key("dedup", namespace, key)


def already_processed(namespace: str, key: str, *, cap: int = _DEFAULT_CAP) -> bool:
    """Return True if ``key`` was previously marked in ``namespace``."""
    if not key:
        return False
    with _lock:
        store = _stores.setdefault(namespace, OrderedDict())
        if key in store:
            store.move_to_end(key)
            return True
        return False


def mark_processed(namespace: str, key: str, *, cap: int = _DEFAULT_CAP) -> None:
    """Record that ``key`` was successfully handled."""
    if not key:
        return
    with _lock:
        store = _stores.setdefault(namespace, OrderedDict())
        store[key] = None
        store.move_to_end(key)
        while len(store) > cap:
            _ = store.popitem(last=False)


def remember_if_new(namespace: str, key: str, *, cap: int = _DEFAULT_CAP) -> bool:
    """Legacy combine: return True if already seen, else mark and return False.

    Prefer ``already_processed`` + ``mark_processed`` after success for new code.
    """
    if already_processed(namespace, key, cap=cap):
        return True
    mark_processed(namespace, key, cap=cap)
    return False


async def already_processed_shared(namespace: str, key: str, *, cap: int = _DEFAULT_CAP) -> bool:
    """True when this event was marked locally or in Redis."""
    if already_processed(namespace, key, cap=cap):
        return True
    if not key or _redis_ttl() <= 0:
        return False
    if await cache_get(_redis_key(namespace, key)) is None:
        return False
    mark_processed(namespace, key, cap=cap)
    return True


async def mark_processed_shared(namespace: str, key: str, *, cap: int = _DEFAULT_CAP) -> None:
    """Record a successful handle in-process and in Redis (SET NX)."""
    mark_processed(namespace, key, cap=cap)
    if not key or _redis_ttl() <= 0:
        return
    _ = await cache_set_nx(_redis_key(namespace, key), "1", ttl=_redis_ttl())
