"""Redis client lifecycle: a durable command pool, a short-timeout cache pool and a Pub/Sub pool of its own.

Each pool is bounded, and running out of connections is its own failure, ``RedisPoolOverloadError``: Redis answered
fine, this process asked for more connections than it may hold. Only the durable command pool waits (up to
``REDIS_COMMAND_WAIT_SECONDS``) for a connection to free up; the cache and Pub/Sub pools fail at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from typing import Final, Literal, Protocol, cast, override

import redis.asyncio as redis
from redis.asyncio.connection import AbstractConnection
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import ConnectionError as RedisConnectionError, MaxConnectionsError, RedisError

from app.config import get_settings
from app.core.json_types import JsonObject

_redis_client: redis.Redis | None = None
_cache_redis_client: redis.Redis | None = None
_pubsub_redis_client: redis.Redis | None = None
_redis_lock: asyncio.Lock | None = None

type _PoolKind = Literal["command", "cache", "pubsub"]

# redis-py reports a timed-out wait for a free connection as a plain ``ConnectionError`` with this message.
_NO_CONNECTION_AVAILABLE: Final = "No connection available."


class RedisPoolOverloadError(RedisError):
    """No pool connection was free within the pool's wait: this process is busy, Redis is not known to be down.

    A ``RedisError`` so a caller that already survives a failed Redis call survives this one too, but neither a
    ``ConnectionError`` nor a ``TimeoutError``: readiness, the fail-open cache circuit and the resubscribe loops
    use that difference and must not treat a busy pool as an outage.
    """


class _CommandPool(redis.BlockingConnectionPool):
    """The durable command pool: waits up to ``timeout`` for a free connection, then fails as pool overload."""

    @override
    async def get_connection(
        self, command_name: str | None = None, *keys: object, **options: object
    ) -> AbstractConnection:
        get_connection = cast("Callable[[], Awaitable[AbstractConnection]]", super().get_connection)
        try:
            return await get_connection()
        except RedisConnectionError as error:
            if str(error) == _NO_CONNECTION_AVAILABLE and isinstance(error.__cause__, TimeoutError):
                raise RedisPoolOverloadError("Redis command pool exhausted") from error
            raise


class _FailFastPool(redis.ConnectionPool):
    """A pool that never waits: no free connection is pool overload at once."""

    @override
    async def get_connection(
        self, command_name: str | None = None, *keys: object, **options: object
    ) -> AbstractConnection:
        get_connection = cast("Callable[[], Awaitable[AbstractConnection]]", super().get_connection)
        try:
            return await get_connection()
        except MaxConnectionsError as error:
            raise RedisPoolOverloadError("Redis pool exhausted") from error


def _lock() -> asyncio.Lock:
    global _redis_lock
    if _redis_lock is None:
        _redis_lock = asyncio.Lock()
    return _redis_lock


def _pool_kwargs(kind: _PoolKind) -> dict[str, object]:
    settings = get_settings()
    connect = float(getattr(settings, "REDIS_SOCKET_CONNECT_TIMEOUT", 2.0) or 2.0)
    health_check = int(getattr(settings, "REDIS_HEALTH_CHECK_INTERVAL", 30) or 30)
    if kind == "cache":
        # Fail fast: a retry would repeat a timed-out command and pin the caller for several socket timeouts, and
        # connecting to a dead host must not outlast the read budget (the socket timeout) either.
        return {
            "decode_responses": True,
            "retry": Retry(NoBackoff(), 0),
            "max_connections": int(getattr(settings, "REDIS_CACHE_MAX_CONNECTIONS", 20) or 20),
            "socket_connect_timeout": float(getattr(settings, "REDIS_CACHE_CONNECT_TIMEOUT", 0.2) or 0.2),
            "socket_timeout": float(getattr(settings, "REDIS_CACHE_SOCKET_TIMEOUT", 0.2) or 0.2),
            "health_check_interval": health_check,
        }
    socket_timeout = float(getattr(settings, "REDIS_SOCKET_TIMEOUT", 5.0) or 5.0)
    if kind == "pubsub":
        return {
            "decode_responses": True,
            "max_connections": int(getattr(settings, "REDIS_PUBSUB_MAX_CONNECTIONS", 4) or 4),
            "socket_connect_timeout": connect,
            "socket_timeout": socket_timeout,
            "health_check_interval": health_check,
        }
    return {
        "decode_responses": True,
        "max_connections": int(getattr(settings, "REDIS_MAX_CONNECTIONS", 50) or 50),
        "timeout": float(getattr(settings, "REDIS_COMMAND_WAIT_SECONDS", 1.0) or 1.0),
        "socket_connect_timeout": connect,
        "socket_timeout": socket_timeout,
        "health_check_interval": health_check,
    }


def _new_client(kind: _PoolKind) -> redis.Redis:
    pool_class = _CommandPool if kind == "command" else _FailFastPool
    from_url = cast("Callable[..., redis.ConnectionPool]", pool_class.from_url)
    pool = from_url(get_settings().REDIS_URL, **_pool_kwargs(kind))
    return redis.Redis.from_pool(pool)


class _RedisEventCommands(Protocol):
    def ping(self) -> Awaitable[bool]: ...
    def publish(self, channel: str, message: str, /) -> Awaitable[int]: ...


def _event_commands(client: redis.Redis) -> _RedisEventCommands:
    return client


def _current_redis() -> redis.Redis | None:
    return _redis_client


async def ping_redis() -> bool:
    """Probe only the lifecycle-created durable client; never initialize from HTTP.

    A command pool with no free connection within its wait is busy, not down: it already holds live connections, so
    the probe reports Redis reachable instead of pulling a loaded process out of rotation.
    """
    client = _current_redis()
    if client is None:
        return False
    try:
        return bool(await _event_commands(client).ping())
    except RedisPoolOverloadError:
        return True


def _current_cache_redis() -> redis.Redis | None:
    return _cache_redis_client


def _current_pubsub_redis() -> redis.Redis | None:
    return _pubsub_redis_client


async def get_redis() -> redis.Redis:
    """Process-global client for tokens, locks, pubsub, and rate limits."""
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    async with _lock():
        _redis_client = _current_redis()
        if _redis_client is None:
            _redis_client = _new_client("command")
        return _redis_client


async def get_cache_redis() -> redis.Redis:
    """Separate pool with a short socket timeout for fail-open caches."""
    global _cache_redis_client
    if _cache_redis_client is not None:
        return _cache_redis_client
    async with _lock():
        _cache_redis_client = _current_cache_redis()
        if _cache_redis_client is None:
            _cache_redis_client = _new_client("cache")
        return _cache_redis_client


async def get_pubsub_redis() -> redis.Redis:
    """Pool for the long-lived Pub/Sub subscribers, so a command burst can never starve a resubscribe."""
    global _pubsub_redis_client
    if _pubsub_redis_client is not None:
        return _pubsub_redis_client
    async with _lock():
        _pubsub_redis_client = _current_pubsub_redis()
        if _pubsub_redis_client is None:
            _pubsub_redis_client = _new_client("pubsub")
        return _pubsub_redis_client


async def publish_event(channel: str, data: JsonObject) -> None:
    """Publish an event to a Redis Pub/Sub channel."""
    commands = _event_commands(await get_redis())
    _ = await commands.publish(channel, json.dumps(data))


async def close_redis() -> None:
    """Close the Redis clients. Each pool is closed independently."""
    global _redis_client, _cache_redis_client, _pubsub_redis_client, _redis_lock
    clients = (_redis_client, _cache_redis_client, _pubsub_redis_client)
    _redis_client = None
    _cache_redis_client = None
    _pubsub_redis_client = None
    _redis_lock = None
    for client in clients:
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()
