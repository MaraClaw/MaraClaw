"""Shared fail-open Redis helpers for short-TTL caches.

Durable callers (password reset, locks, pubsub, webhooks) must keep using
``get_redis()``. This module is only for caches that may fall back to the
source of truth when Redis is slow or down.
"""

from __future__ import annotations

import json
import time
from contextvars import ContextVar, Token

from app.config import get_settings
from app.core.logging import logger

_COOLDOWN_SECONDS = 2.0
_cache_dead_until = 0.0
_deferred_incrs: ContextVar[set[str] | None] = ContextVar("redis_cache_deferred", default=None)
_deferred_deletes: ContextVar[set[str] | None] = ContextVar("redis_cache_deferred_deletes", default=None)
_delete_tokens: ContextVar[tuple[Token[set[str] | None], ...]] = ContextVar(
    "redis_cache_deferred_delete_tokens",
    default=(),
)


def cache_prefix() -> str:
    return str(getattr(get_settings(), "REDIS_KEY_PREFIX", "mrc") or "mrc")


def cache_key(*parts: object) -> str:
    """Build a namespaced Redis key (``mc:sess:v1:…``)."""
    return ":".join((cache_prefix(), *(str(part) for part in parts)))


def _max_bytes() -> int:
    return int(getattr(get_settings(), "REDIS_CACHE_MAX_VALUE_BYTES", 65536) or 65536)


def _cooling_down() -> bool:
    return time.monotonic() < _cache_dead_until


def _trip() -> None:
    global _cache_dead_until
    _cache_dead_until = time.monotonic() + _COOLDOWN_SECONDS


def reset_circuit() -> None:
    """Test helper: clear the fail-open cooldown."""
    global _cache_dead_until
    _cache_dead_until = 0.0


async def _client():
    from app.core.events import get_cache_redis

    return await get_cache_redis()


async def cache_get(key: str) -> str | None:
    """Return a string value, or None on miss / disable / Redis error."""
    if _cooling_down():
        return None
    try:
        client = await _client()
        value = await client.get(key)
    except Exception as exc:
        _trip()
        logger.debug("redis_cache get skipped: {}", type(exc).__name__)
        return None
    if isinstance(value, bytes):
        return value.decode()
    return value if isinstance(value, str) else None


async def cache_set(key: str, value: str, *, ttl: int) -> bool:
    """SET with expiry. Returns False when skipped (ttl, size, or Redis error)."""
    if ttl <= 0 or _cooling_down():
        return False
    if len(value.encode()) > _max_bytes():
        logger.debug("redis_cache set skipped (too large)")
        return False
    try:
        client = await _client()
        _ = await client.set(key, value, ex=ttl)
    except Exception as exc:
        _trip()
        logger.debug("redis_cache set skipped: {}", type(exc).__name__)
        return False
    return True


async def cache_set_nx(key: str, value: str, *, ttl: int) -> bool:
    """SET NX with expiry. True only when this caller created the key."""
    if ttl <= 0 or _cooling_down():
        return False
    try:
        client = await _client()
        return bool(await client.set(key, value, ex=ttl, nx=True))
    except Exception as exc:
        _trip()
        logger.debug("redis_cache set_nx skipped: {}", type(exc).__name__)
        return False


async def cache_delete(*keys: str) -> None:
    """Always attempt DELETE. Invalidation must not be swallowed by the read circuit."""
    if not keys:
        return
    try:
        client = await _client()
        _ = await client.delete(*keys)
    except Exception as exc:
        logger.debug("redis_cache delete skipped: {}", type(exc).__name__)


async def defer_cache_delete(*keys: str) -> None:
    """Delete cache keys now, or after the open request transaction commits.

    Session and tenant payload keys are not version checks by themselves.
    Dropping them before commit would publish an invalidation that a rolled
    back delete cannot take back. Callers in the same request should use
    ``deletion_is_pending`` so they do not keep reading the stale payload.
    """
    pending = _deferred_deletes.get()
    if pending is None:
        await cache_delete(*keys)
        return
    pending.update(key for key in keys if key)


def deletion_is_pending(key: str) -> bool:
    """Return whether ``key`` is waiting for the request transaction to commit."""
    pending = _deferred_deletes.get()
    return pending is not None and key in pending


async def cache_get_json(key: str) -> object | None:
    raw = await cache_get(key)
    if raw is None:
        return None
    try:
        from app.core.json_types import json_loads_value

        return json_loads_value(raw)
    except TypeError, ValueError:
        return None


async def cache_set_json(key: str, value: object, *, ttl: int) -> bool:
    try:
        payload = json.dumps(value, default=_json_default, separators=(",", ":"))
    except TypeError, ValueError:
        return False
    return await cache_set(key, payload, ttl=ttl)


def _json_default(value: object) -> object:
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return isoformat()
    return str(value)


async def read_version(version_key: str) -> str:
    raw = await cache_get(version_key)
    return str(raw or "0")


async def bump_version(version_key: str, *, ttl: int | None = None) -> None:
    """INCR a version token. Deferred until commit when a connection_ctx is open."""
    pending = _deferred_incrs.get()
    if pending is not None:
        pending.add(version_key)
        return
    await _incr_version_now(version_key, ttl=ttl)


async def drop_version(version_key: str) -> None:
    await cache_delete(version_key)


async def _incr_version_now(version_key: str, *, ttl: int | None = None) -> None:
    expire_for = ttl if ttl and ttl > 0 else 3600
    try:
        client = await _client()
        _ = await client.incr(version_key)
        expire = getattr(client, "expire", None)
        if expire is not None:
            await expire(version_key, max(expire_for, 3600))
    except Exception as exc:
        logger.debug("redis_cache incr skipped: {}", type(exc).__name__)


def begin_deferred_versions() -> Token[set[str] | None]:
    pending: set[str] = set()
    delete_token = _deferred_deletes.set(set())
    _delete_tokens.set((*_delete_tokens.get(), delete_token))
    return _deferred_incrs.set(pending)


def end_deferred_versions(token: Token[set[str] | None]) -> None:
    _deferred_incrs.reset(token)
    tokens = _delete_tokens.get()
    if not tokens:
        return
    _delete_tokens.set(tokens[:-1])
    _deferred_deletes.reset(tokens[-1])


async def flush_deferred_versions() -> None:
    pending = _deferred_incrs.get()
    if pending:
        keys = list(pending)
        pending.clear()
        for key in keys:
            await _incr_version_now(key)
    deletes = _deferred_deletes.get()
    if not deletes:
        return
    delete_keys = list(deletes)
    deletes.clear()
    if delete_keys:
        await cache_delete(*delete_keys)
