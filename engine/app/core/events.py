"""Redis client lifecycle: durable Pub/Sub + a short-timeout cache pool."""

from __future__ import annotations

import asyncio
import contextlib
import json

import redis.asyncio as redis

from app.config import get_settings
from app.core.json_types import JsonObject

_redis_client: redis.Redis | None = None
_cache_redis_client: redis.Redis | None = None
_redis_lock: asyncio.Lock | None = None


def _lock() -> asyncio.Lock:
    global _redis_lock
    if _redis_lock is None:
        _redis_lock = asyncio.Lock()
    return _redis_lock


def _pool_kwargs(*, cache: bool) -> dict[str, object]:
    settings = get_settings()
    connect = float(getattr(settings, "REDIS_SOCKET_CONNECT_TIMEOUT", 2.0) or 2.0)
    if cache:
        return {
            "decode_responses": True,
            "max_connections": int(getattr(settings, "REDIS_CACHE_MAX_CONNECTIONS", 20) or 20),
            "socket_connect_timeout": connect,
            "socket_timeout": float(getattr(settings, "REDIS_CACHE_SOCKET_TIMEOUT", 0.2) or 0.2),
            "health_check_interval": int(getattr(settings, "REDIS_HEALTH_CHECK_INTERVAL", 30) or 30),
        }
    return {
        "decode_responses": True,
        "max_connections": int(getattr(settings, "REDIS_MAX_CONNECTIONS", 50) or 50),
        "socket_connect_timeout": connect,
        "socket_timeout": float(getattr(settings, "REDIS_SOCKET_TIMEOUT", 5.0) or 5.0),
        "health_check_interval": int(getattr(settings, "REDIS_HEALTH_CHECK_INTERVAL", 30) or 30),
    }


async def get_redis() -> redis.Redis:
    """Process-global client for tokens, locks, pubsub, and rate limits."""
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    async with _lock():
        if _redis_client is None:
            settings = get_settings()
            _redis_client = redis.from_url(settings.REDIS_URL, **_pool_kwargs(cache=False))
        return _redis_client


async def get_cache_redis() -> redis.Redis:
    """Separate pool with a short socket timeout for fail-open caches."""
    global _cache_redis_client
    if _cache_redis_client is not None:
        return _cache_redis_client
    async with _lock():
        if _cache_redis_client is None:
            settings = get_settings()
            _cache_redis_client = redis.from_url(settings.REDIS_URL, **_pool_kwargs(cache=True))
        return _cache_redis_client


async def publish_event(channel: str, data: JsonObject) -> None:
    """Publish an event to a Redis Pub/Sub channel."""
    r = await get_redis()
    _ = await r.publish(channel, json.dumps(data))


async def close_redis() -> None:
    """Close both Redis clients. Each pool is closed independently."""
    global _redis_client, _cache_redis_client, _redis_lock
    durable = _redis_client
    cache = _cache_redis_client
    _redis_client = None
    _cache_redis_client = None
    _redis_lock = None
    if durable is not None:
        with contextlib.suppress(Exception):
            await durable.aclose()
    if cache is not None:
        with contextlib.suppress(Exception):
            await cache.aclose()
