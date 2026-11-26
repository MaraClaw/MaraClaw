"""Shared fail-open Redis helpers for short-TTL caches.

Durable callers (password reset, locks, pubsub, webhooks) must keep using
``get_redis()``. This module is only for caches that may fall back to the
source of truth when Redis is slow or down.

Every fail-open cache *read or fill* goes through one breaker (``guarded``): a failure opens it, calls are then
refused at once, and after the cooldown a single probe decides whether it closes again. Invalidation writes never
consult it.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from enum import StrEnum
from typing import final
from uuid import uuid4

from app.config import get_settings
from app.core.cache_contracts import RedisValueRead
from app.core.events import RedisPoolOverloadError
from app.core.logging import logger
from app.records.cache_invalidation import CacheInvalidation, CacheInvalidationKind

_COOLDOWN_SECONDS = 2.0
_VERSION_MIN_TTL = 3600


class _Admission(StrEnum):
    CALL = "call"
    PROBE = "probe"


class CacheUnavailableError(Exception):
    """A guarded cache call was refused by the open breaker or failed; the caller falls back to the source of truth."""


@final
class CacheBreaker:
    """Closed, open or half-open circuit for the cache Redis; the clock is injectable for tests.

    A failure opens it for ``cooldown`` seconds. Past the cooldown exactly one caller is admitted as the probe while
    every other call is still refused: a successful probe closes the circuit, a failed one reopens it for another
    cooldown, and a probe that proved nothing (cancelled, busy pool) hands the slot to the next caller.
    """

    def __init__(self, *, cooldown: float = _COOLDOWN_SECONDS, clock: Callable[[], float] = time.monotonic) -> None:
        self._cooldown = cooldown
        self._clock = clock
        self._opened = False
        self._probing = False
        self._retry_at = 0.0

    def admit(self) -> _Admission | None:
        """Admit the caller, or return None to refuse it."""
        if not self._opened:
            return _Admission.CALL
        if self._probing or self._clock() < self._retry_at:
            return None
        self._probing = True
        return _Admission.PROBE

    def refusing(self) -> bool:
        """Whether a call made right now would be refused."""
        return self._opened and (self._probing or self._clock() < self._retry_at)

    def trip(self) -> None:
        self._opened = True
        self._probing = False
        self._retry_at = self._clock() + self._cooldown

    def succeeded(self, admission: _Admission) -> None:
        if admission is _Admission.PROBE:
            self.reset()

    def failed(self, admission: _Admission) -> None:
        # A straggler admitted before the circuit opened says nothing new; only the probe may reopen it.
        if admission is _Admission.PROBE or not self._opened:
            self.trip()

    def abandoned(self, admission: _Admission) -> None:
        if admission is _Admission.PROBE:
            self._probing = False

    def reset(self) -> None:
        self._opened = False
        self._probing = False
        self._retry_at = 0.0


_breaker = CacheBreaker()


@dataclass(frozen=True, slots=True)
class _DeferredVersions:
    incrs: set[str] = field(default_factory=set)
    deletes: set[str] = field(default_factory=set)


_deferred_versions: ContextVar[_DeferredVersions | None] = ContextVar("redis_cache_deferred", default=None)


def cache_prefix() -> str:
    return str(getattr(get_settings(), "REDIS_KEY_PREFIX", "mrc") or "mrc")


def cache_key(*parts: object) -> str:
    """Build a namespaced Redis key (``mc:sess:v1:…``)."""
    return ":".join((cache_prefix(), *(str(part) for part in parts)))


def _max_bytes() -> int:
    return int(getattr(get_settings(), "REDIS_CACHE_MAX_VALUE_BYTES", 65536) or 65536)


def _cooling_down() -> bool:
    return _breaker.refusing()


def _trip() -> None:
    _breaker.trip()


def reset_circuit() -> None:
    """Test helper: close the fail-open circuit."""
    _breaker.reset()


async def guarded[T](call: Callable[[], Awaitable[T]]) -> T:
    """Run one fail-open cache round trip under the breaker; raise ``CacheUnavailableError`` instead of waiting on a dead Redis.

    ``call`` does only the Redis I/O (parse its reply outside). Any failure but a busy pool opens the circuit; a busy
    pool (``RedisPoolOverloadError``) fails just this call, because Redis is not known to be down.
    """
    admission = _breaker.admit()
    if admission is None:
        raise CacheUnavailableError("circuit open")
    try:
        result = await call()
    except RedisPoolOverloadError as exc:
        _breaker.abandoned(admission)
        raise CacheUnavailableError(type(exc).__name__) from exc
    except Exception as exc:
        _breaker.failed(admission)
        raise CacheUnavailableError(type(exc).__name__) from exc
    except BaseException:
        _breaker.abandoned(admission)
        raise
    _breaker.succeeded(admission)
    return result


async def _client():
    from app.core.events import get_cache_redis

    return await get_cache_redis()


async def cache_get(key: str) -> str | None:
    """Return a string value, or None on miss / disable / Redis error."""
    result = await _read_value(key)
    return result.value if result is not None else None


async def _read_value(key: str) -> RedisValueRead | None:
    async def read() -> object:
        return await (await _client()).get(key)

    try:
        value = await guarded(read)
    except CacheUnavailableError as exc:
        logger.debug("redis_cache get skipped: {}", exc)
        return None
    if isinstance(value, bytes):
        return RedisValueRead(value.decode())
    return RedisValueRead(value if isinstance(value, str) else None)


async def cache_set(key: str, value: str, *, ttl: int) -> bool:
    """SET with expiry. Returns False when skipped (ttl, size, or Redis error)."""
    if ttl <= 0:
        return False
    if len(value.encode()) > _max_bytes():
        logger.debug("redis_cache set skipped (too large)")
        return False

    async def write() -> object:
        return await (await _client()).set(key, value, ex=ttl)

    try:
        _ = await guarded(write)
    except CacheUnavailableError as exc:
        logger.debug("redis_cache set skipped: {}", exc)
        return False
    return True


async def cache_set_nx(key: str, value: str, *, ttl: int) -> bool:
    """SET NX with expiry. True only when this caller created the key."""
    if ttl <= 0:
        return False

    async def claim() -> object:
        return await (await _client()).set(key, value, ex=ttl, nx=True)

    try:
        return bool(await guarded(claim))
    except CacheUnavailableError as exc:
        logger.debug("redis_cache set_nx skipped: {}", exc)
        return False


async def cache_delete(*keys: str, defer_until_commit: bool = False) -> None:
    """Delete outside the read circuit; optionally defer snapshots to the SQL commit."""
    if not keys:
        return
    pending = _deferred_versions.get()
    if defer_until_commit and pending is not None:
        pending.deletes.update(keys)
        return
    await _delete_now(*keys)


async def _delete_now(*keys: str) -> None:
    try:
        client = await _client()
        _ = await client.delete(*keys)
    except Exception as exc:
        logger.warning("redis_cache delete failed: keys={} error={}", len(keys), type(exc).__name__)


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


async def read_version(version_key: str) -> RedisValueRead | None:
    """Separate an unavailable read (None) from a successful miss (value=None)."""
    return await _read_value(version_key)


async def ensure_version(version_key: str, *, ttl: int) -> str | None:
    """Establish an opaque generation before loading the authoritative SQL row."""
    result = await read_version(version_key)
    if result is None:
        return None
    if result.value is not None:
        return result.value
    generation = uuid4().hex
    if await cache_set_nx(version_key, generation, ttl=max(ttl, 3600)):
        return generation
    current = await read_version(version_key)
    return current.value if current is not None else None


async def bump_version(version_key: str, *, ttl: int | None = None) -> None:
    """Rotate a generation. Deferred until commit when a connection_ctx is open."""
    pending = _deferred_versions.get()
    if pending is not None:
        pending.incrs.add(version_key)
        return
    await _incr_version_now(version_key, ttl=ttl)


async def drop_version(version_key: str) -> None:
    await cache_delete(version_key)


async def _incr_version_now(version_key: str, *, ttl: int | None = None) -> None:
    expire_for = ttl if ttl not in (None, 0) and ttl > 0 else _VERSION_MIN_TTL
    try:
        client = await _client()
        _ = await client.set(version_key, uuid4().hex, ex=max(expire_for, _VERSION_MIN_TTL))
    except Exception as exc:
        logger.warning("redis_cache version bump failed: keys=1 error={}", type(exc).__name__)


def has_pending_snapshot_writes() -> bool:
    pending = _deferred_versions.get()
    return pending is not None and bool(pending.incrs or pending.deletes)


def pending_snapshot_invalidations() -> tuple[CacheInvalidation, ...]:
    """Snapshot deferred intent without mutating the request's dirty-read fence."""
    pending = _deferred_versions.get()
    if pending is None:
        return ()
    return tuple(CacheInvalidation(key, CacheInvalidationKind.VERSION) for key in pending.incrs) + tuple(
        CacheInvalidation(key, CacheInvalidationKind.DELETE) for key in pending.deletes
    )


def begin_deferred_versions() -> Token[_DeferredVersions | None]:
    return _deferred_versions.set(_DeferredVersions())


def end_deferred_versions(token: Token[_DeferredVersions | None]) -> None:
    _deferred_versions.reset(token)


async def flush_deferred_versions() -> None:
    pending = _deferred_versions.get()
    if pending is None:
        return
    bumps = tuple(pending.incrs)
    deletes = tuple(pending.deletes)
    pending.incrs.clear()
    pending.deletes.clear()
    if not bumps and not deletes:
        return
    try:
        client = await _client()
        async with client.pipeline(transaction=False) as pipe:
            for key in bumps:
                _ = pipe.set(key, uuid4().hex, ex=_VERSION_MIN_TTL)
            if deletes:
                _ = pipe.delete(*deletes)
            _ = await pipe.execute()
    except Exception as exc:
        logger.warning("redis_cache flush failed: keys={} error={}", len(bumps) + len(deletes), type(exc).__name__)
