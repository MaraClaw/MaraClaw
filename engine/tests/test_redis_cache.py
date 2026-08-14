"""Fail-open Redis cache helper + pool construction."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.core import events, redis_cache
from app.core.row_memo import clear_row_memo, memo_drop, memo_get, memo_set


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.fail = False
        self.expires: dict[str, int] = {}

    async def get(self, key: str) -> str | None:
        if self.fail:
            raise RuntimeError("redis down")
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool:
        if self.fail:
            raise RuntimeError("redis down")
        if nx and key in self.store:
            return False
        self.store[key] = value
        if ex is not None:
            self.expires[key] = ex
        return True

    async def delete(self, *keys: str) -> None:
        if self.fail:
            raise RuntimeError("redis down")
        for key in keys:
            self.store.pop(key, None)

    async def incr(self, key: str) -> int:
        if self.fail:
            raise RuntimeError("redis down")
        nxt = int(self.store.get(key) or 0) + 1
        self.store[key] = str(nxt)
        return nxt

    async def expire(self, key: str, ttl: int) -> None:
        if self.fail:
            raise RuntimeError("redis down")
        self.expires[key] = ttl


@pytest.fixture
def fake_cache(monkeypatch: pytest.MonkeyPatch):
    redis_cache.reset_circuit()
    client = FakeRedis()

    async def get_cache_redis() -> FakeRedis:
        return client

    monkeypatch.setattr(redis_cache, "_client", get_cache_redis)
    yield client
    redis_cache.reset_circuit()


@pytest.mark.asyncio
async def test_cache_get_set_roundtrip(fake_cache: FakeRedis) -> None:
    assert await redis_cache.cache_get("k") is None
    assert await redis_cache.cache_set("k", "v", ttl=30) is True
    assert await redis_cache.cache_get("k") == "v"


@pytest.mark.asyncio
async def test_cache_set_nx(fake_cache: FakeRedis) -> None:
    assert await redis_cache.cache_set_nx("lock", "1", ttl=10) is True
    assert await redis_cache.cache_set_nx("lock", "2", ttl=10) is False
    assert fake_cache.store["lock"] == "1"


@pytest.mark.asyncio
async def test_cache_json_and_version(fake_cache: FakeRedis) -> None:
    assert await redis_cache.cache_set_json("j", {"a": 1}, ttl=10) is True
    assert await redis_cache.cache_get_json("j") == {"a": 1}
    assert await redis_cache.read_version("ver") == "0"
    await redis_cache.bump_version("ver", ttl=10)
    assert await redis_cache.read_version("ver") == "1"
    assert fake_cache.expires["ver"] >= 3600


@pytest.mark.asyncio
async def test_cache_fail_open(fake_cache: FakeRedis) -> None:
    fake_cache.fail = True
    assert await redis_cache.cache_get("k") is None
    assert await redis_cache.cache_set("k", "v", ttl=10) is False
    assert await redis_cache.cache_set_nx("k", "v", ttl=10) is False


@pytest.mark.asyncio
async def test_circuit_does_not_block_invalidation(fake_cache: FakeRedis) -> None:
    await redis_cache.cache_set("sess", "old", ttl=10)
    redis_cache._trip()
    await redis_cache.cache_delete("sess")
    await redis_cache.bump_version("ver", ttl=10)
    assert "sess" not in fake_cache.store
    assert fake_cache.store["ver"] == "1"


@pytest.mark.asyncio
async def test_cache_rejects_oversized(fake_cache: FakeRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(redis_cache, "_max_bytes", lambda: 8)
    assert await redis_cache.cache_set("k", "0123456789", ttl=10) is False
    assert "k" not in fake_cache.store


@pytest.mark.asyncio
async def test_deferred_version_flush(fake_cache: FakeRedis) -> None:
    token = redis_cache.begin_deferred_versions()
    await redis_cache.bump_version("ver")
    assert "ver" not in fake_cache.store
    await redis_cache.flush_deferred_versions()
    assert fake_cache.store["ver"] == "1"
    redis_cache.end_deferred_versions(token)


def test_row_memo_set_get_drop() -> None:
    clear_row_memo()
    assert memo_get("agent", "abc") is None
    memo_set("agent", "abc", "row")
    assert memo_get("agent", "abc") == "row"
    memo_drop("agent", "abc")
    assert memo_get("agent", "abc") is None
    clear_row_memo()


@pytest.mark.asyncio
async def test_get_redis_passes_pool_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, Any]] = []

    def fake_from_url(url: str, **kwargs: Any) -> object:
        captured.append({"url": url, **kwargs})
        return SimpleNamespace()

    monkeypatch.setattr(events, "_redis_client", None)
    monkeypatch.setattr(events, "_cache_redis_client", None)
    monkeypatch.setattr(events.redis, "from_url", fake_from_url)
    monkeypatch.setattr(
        events,
        "get_settings",
        lambda: SimpleNamespace(
            REDIS_URL="redis://example:6379/0",
            REDIS_MAX_CONNECTIONS=40,
            REDIS_CACHE_MAX_CONNECTIONS=12,
            REDIS_SOCKET_CONNECT_TIMEOUT=1.5,
            REDIS_SOCKET_TIMEOUT=4.0,
            REDIS_CACHE_SOCKET_TIMEOUT=0.25,
            REDIS_HEALTH_CHECK_INTERVAL=15,
        ),
    )

    await events.get_redis()
    await events.get_cache_redis()
    assert captured[0]["max_connections"] == 40
    assert captured[0]["socket_timeout"] == 4.0
    assert captured[1]["max_connections"] == 12
    assert captured[1]["socket_timeout"] == 0.25
    events._redis_client = None
    events._cache_redis_client = None
