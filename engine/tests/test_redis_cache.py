"""Fail-open Redis cache helper + pool construction."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Self
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError
from redis.exceptions import ConnectionError as RedisConnectionError, RedisError, TimeoutError as RedisTimeoutError

from app.config import Settings
from app.core import events, redis_cache
from app.core.cache_contracts import RedisValueRead, SessionKeysRead
from app.core.row_memo import clear_row_memo, memo_drop, memo_get, memo_set
from app.core.single_flight import SingleFlight


class FakePipeline:
    def __init__(self, parent: FakeRedis) -> None:
        self._parent = parent
        self._ops: list[tuple[str, tuple[str, ...], int | None]] = []
        self._nx: list[bool] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> FakePipeline:
        self._ops.append(("set", (key, value), ex))
        self._nx.append(nx)
        return self

    def get(self, key: str) -> FakePipeline:
        self._ops.append(("get", (key,), None))
        self._nx.append(False)
        return self

    def delete(self, *keys: str) -> FakePipeline:
        self._ops.append(("delete", keys, None))
        self._nx.append(False)
        return self

    async def execute(self) -> list[bool | str | None]:
        parent = self._parent
        parent.round_trips += 1
        parent.commands.append("pipeline")
        if parent.fail:
            raise RuntimeError("redis down")
        parent.pipelines.append(self._ops)
        results: list[bool | str | None] = []
        for (op, args, ex), nx in zip(self._ops, self._nx, strict=True):
            if op == "set":
                results.append(parent.write(args[0], args[1], ex=ex, nx=nx))
            elif op == "get":
                results.append(parent.read(args[0]))
            else:
                parent.remove(*args)
                results.append(True)
        return results


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.fail: bool = False
        self.fail_gets: set[str] = set()
        self.expires: dict[str, int] = {}
        self.pipelines: list[list[tuple[str, tuple[str, ...], int | None]]] = []
        self.round_trips: int = 0
        self.commands: list[str] = []

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        return FakePipeline(self)

    def read(self, key: str) -> str | None:
        if self.fail or key in self.fail_gets:
            raise RuntimeError("redis down")
        return self.store.get(key)

    def write(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool:
        if self.fail:
            raise RuntimeError("redis down")
        if nx and key in self.store:
            return False
        self.store[key] = value
        if ex is not None:
            self.expires[key] = ex
        return True

    def remove(self, *keys: str) -> None:
        if self.fail:
            raise RuntimeError("redis down")
        for key in keys:
            self.store.pop(key, None)

    async def get(self, key: str) -> str | None:
        self.round_trips += 1
        self.commands.append("get")
        return self.read(key)

    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool:
        self.round_trips += 1
        self.commands.append("set")
        return self.write(key, value, ex=ex, nx=nx)

    async def delete(self, *keys: str) -> None:
        self.round_trips += 1
        self.commands.append("delete")
        self.remove(*keys)

    async def incr(self, key: str) -> int:
        self.round_trips += 1
        self.commands.append("incr")
        if self.fail:
            raise RuntimeError("redis down")
        nxt = int(self.store.get(key) or 0) + 1
        self.store[key] = str(nxt)
        return nxt

    async def expire(self, key: str, ttl: int) -> None:
        self.round_trips += 1
        self.commands.append("expire")
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
    assert await redis_cache.read_version("ver") == RedisValueRead(None)
    await redis_cache.bump_version("ver", ttl=10)
    assert await redis_cache.read_version("ver") == RedisValueRead(fake_cache.store["ver"])
    assert fake_cache.expires["ver"] >= 3600


@pytest.mark.asyncio
async def test_cache_fail_open(fake_cache: FakeRedis) -> None:
    fake_cache.fail = True
    assert await redis_cache.cache_get("k") is None
    assert await redis_cache.cache_set("k", "v", ttl=10) is False
    assert await redis_cache.cache_set_nx("k", "v", ttl=10) is False


async def test_version_failure_differs_from_successfully_missing_generation(fake_cache: FakeRedis) -> None:
    missing = await redis_cache.read_version("ver")
    fake_cache.fail_gets.add("ver")

    failed = await redis_cache.read_version("ver")

    assert failed != missing


async def test_session_keys_are_one_pipeline_of_single_key_gets_each_named_by_the_caller(
    fake_cache: FakeRedis,
) -> None:
    fake_cache.store.update({"snap": json.dumps({"user": {"id": "u"}}), "uver": "1", "iver": "2"})

    keys = await redis_cache.read_session_keys("snap", "uver", "iver")

    assert keys == SessionKeysRead({"user": {"id": "u"}}, "1", "2")
    assert (fake_cache.commands, fake_cache.round_trips) == (["pipeline"], 1)
    assert fake_cache.pipelines == [[("get", ("snap",), None), ("get", ("uver",), None), ("get", ("iver",), None)]]


async def test_session_keys_without_an_identity_key_read_two_keys_and_no_identity_generation(
    fake_cache: FakeRedis,
) -> None:
    fake_cache.store.update({"snap": "{}", "uver": "1", "iver": "2"})

    keys = await redis_cache.read_session_keys("snap", "uver", None)

    assert keys == SessionKeysRead({}, "1", None)
    assert fake_cache.pipelines == [[("get", ("snap",), None), ("get", ("uver",), None)]]


@pytest.mark.parametrize("stored", [None, "{not json"])
async def test_a_missing_or_unparseable_snapshot_reads_as_no_snapshot_beside_the_generations(
    fake_cache: FakeRedis, stored: str | None
) -> None:
    fake_cache.store.update({"uver": "1", "iver": "2"})
    if stored is not None:
        fake_cache.store["snap"] = stored

    assert await redis_cache.read_session_keys("snap", "uver", "iver") == SessionKeysRead(None, "1", "2")
    assert fake_cache.round_trips == 1


@pytest.mark.parametrize("failing", ["snap", "uver", "iver"])
async def test_a_failed_session_read_is_unavailable_and_opens_the_breaker(
    fake_cache: FakeRedis, breaker: tuple[redis_cache.CacheBreaker, _Clock], failing: str
) -> None:
    fake_cache.store.update({"snap": "{}", "uver": "1", "iver": "2"})
    fake_cache.fail_gets.add(failing)

    assert await redis_cache.read_session_keys("snap", "uver", "iver") is None

    fake_cache.fail_gets.clear()
    assert breaker[0].refusing()
    assert await redis_cache.read_session_keys("snap", "uver", "iver") is None
    assert fake_cache.round_trips == 1


async def test_generations_are_stable_until_rotated(fake_cache: FakeRedis) -> None:
    initial = await redis_cache.ensure_version("ver", ttl=10)
    assert initial is not None
    assert await redis_cache.ensure_version("ver", ttl=10) == initial

    await redis_cache.bump_version("ver")

    current = await redis_cache.read_version("ver")
    assert current is not None
    assert current.value is not None
    assert current.value != initial


async def test_generation_initialization_preserves_competing_writer(
    fake_cache: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def competing_set(key: str, value: str, *, ttl: int) -> bool:
        fake_cache.store[key] = "competing-generation"
        return False

    monkeypatch.setattr(redis_cache, "cache_set_nx", competing_set)

    observed = await redis_cache.ensure_version("ver", ttl=10)

    assert observed == "competing-generation"


@pytest.mark.asyncio
async def test_circuit_does_not_block_invalidation(fake_cache: FakeRedis) -> None:
    await redis_cache.cache_set("sess", "old", ttl=10)
    redis_cache._trip()
    await redis_cache.cache_delete("sess")
    await redis_cache.bump_version("ver", ttl=10)
    assert "sess" not in fake_cache.store
    assert fake_cache.store["ver"]


@pytest.mark.asyncio
async def test_cache_rejects_oversized(fake_cache: FakeRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(redis_cache, "_max_bytes", lambda: 8)
    assert await redis_cache.cache_set("k", "0123456789", ttl=10) is False
    assert "k" not in fake_cache.store


@pytest.mark.asyncio
@pytest.mark.parametrize("defer_until_commit", [False, True])
async def test_cache_delete_is_immediate_when_outside_transaction(
    fake_cache: FakeRedis, defer_until_commit: bool
) -> None:
    # Given
    fake_cache.store.update(first="old", second="old")
    # When
    await redis_cache.cache_delete("first", "second", defer_until_commit=defer_until_commit)
    # Then
    assert fake_cache.store == {}


@pytest.mark.asyncio
async def test_snapshot_delete_waits_when_versions_are_deferred(fake_cache: FakeRedis) -> None:
    # Given
    fake_cache.store["snapshot"] = "old"
    token = redis_cache.begin_deferred_versions()
    try:
        # When
        await redis_cache.cache_delete("snapshot", defer_until_commit=True)
        # Then
        assert fake_cache.store == {"snapshot": "old"}
    finally:
        redis_cache.end_deferred_versions(token)


@pytest.mark.asyncio
async def test_snapshot_delete_flushes_when_read_circuit_is_open(fake_cache: FakeRedis) -> None:
    # Given
    fake_cache.store["snapshot"] = "old"
    token = redis_cache.begin_deferred_versions()
    try:
        await redis_cache.cache_delete("snapshot", defer_until_commit=True)
        redis_cache._trip()
        # When
        await redis_cache.flush_deferred_versions()
        # Then
        assert fake_cache.store == {}
    finally:
        redis_cache.end_deferred_versions(token)


@pytest.mark.asyncio
async def test_generic_delete_stays_immediate_when_versions_are_deferred(fake_cache: FakeRedis) -> None:
    # Given
    fake_cache.store["token"] = "old"
    token = redis_cache.begin_deferred_versions()
    try:
        # When
        await redis_cache.cache_delete("token")
        # Then
        assert fake_cache.store == {}
    finally:
        redis_cache.end_deferred_versions(token)


@pytest.mark.asyncio
async def test_deferred_version_flush(fake_cache: FakeRedis) -> None:
    token = redis_cache.begin_deferred_versions()
    await redis_cache.bump_version("ver")
    assert "ver" not in fake_cache.store
    await redis_cache.flush_deferred_versions()
    assert fake_cache.store["ver"]
    redis_cache.end_deferred_versions(token)


@pytest.mark.asyncio
async def test_deferred_flush_is_one_pipeline_that_expires_every_rotated_version(fake_cache: FakeRedis) -> None:
    # Given two rotated generations and a snapshot delete queued in one scope
    token = redis_cache.begin_deferred_versions()
    try:
        await redis_cache.bump_version("ver:a", ttl=10)
        await redis_cache.bump_version("ver:b")
        await redis_cache.cache_delete("snapshot", defer_until_commit=True)
        fake_cache.store["snapshot"] = "old"
        # When
        await redis_cache.flush_deferred_versions()
    finally:
        redis_cache.end_deferred_versions(token)
    # Then one round trip carried every write, each version with its own expiry
    assert len(fake_cache.pipelines) == 1
    sets = {args[0]: ex for op, args, ex in fake_cache.pipelines[0] if op == "set"}
    assert sets == {"ver:a": 3600, "ver:b": 3600}
    assert fake_cache.pipelines[0][-1] == ("delete", ("snapshot",), None)
    assert fake_cache.expires == {"ver:a": 3600, "ver:b": 3600}
    assert "snapshot" not in fake_cache.store
    assert fake_cache.store["ver:a"] != fake_cache.store["ver:b"]


@pytest.mark.asyncio
async def test_deferred_flush_with_nothing_queued_sends_no_pipeline(fake_cache: FakeRedis) -> None:
    token = redis_cache.begin_deferred_versions()
    try:
        await redis_cache.flush_deferred_versions()
    finally:
        redis_cache.end_deferred_versions(token)
    assert fake_cache.pipelines == []


@pytest.mark.asyncio
async def test_durable_capture_snapshot_preserves_dirty_fence_and_deduplicates(fake_cache: FakeRedis) -> None:
    token = redis_cache.begin_deferred_versions()
    try:
        for _ in range(2):
            await redis_cache.bump_version("ver:durable")
            await redis_cache.cache_delete("snap:durable", defer_until_commit=True)
        captured = redis_cache.pending_snapshot_invalidations()
        assert {(op.key, op.kind) for op in captured} == {("ver:durable", "version"), ("snap:durable", "delete")}
        assert redis_cache.pending_snapshot_invalidations() == captured
        assert redis_cache.has_pending_snapshot_writes()
        assert fake_cache.pipelines == []
        await redis_cache.flush_deferred_versions()
        assert redis_cache.pending_snapshot_invalidations() == ()
    finally:
        redis_cache.end_deferred_versions(token)


@pytest.mark.asyncio
async def test_open_circuit_never_suppresses_a_version_flush(fake_cache: FakeRedis) -> None:
    # Given a tripped read circuit
    token = redis_cache.begin_deferred_versions()
    try:
        await redis_cache.bump_version("ver")
        redis_cache._trip()
        # When
        await redis_cache.flush_deferred_versions()
    finally:
        redis_cache.end_deferred_versions(token)
    # Then the rotation was still published
    assert fake_cache.store["ver"]
    assert len(fake_cache.pipelines) == 1


@pytest.mark.asyncio
async def test_failed_invalidations_log_warnings_and_do_not_trip_the_circuit(
    fake_cache: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a Redis that rejects every command
    logger = Mock()
    monkeypatch.setattr(redis_cache, "logger", logger)
    fake_cache.fail = True
    token = redis_cache.begin_deferred_versions()
    try:
        await redis_cache.bump_version("ver")
        await redis_cache.cache_delete("snapshot", defer_until_commit=True)
        # When the deferred batch is flushed
        await redis_cache.flush_deferred_versions()
    finally:
        redis_cache.end_deferred_versions(token)
    # Then one WARNING covers the batch
    assert logger.warning.call_count == 1
    # And immediate invalidations warn too, without being gated by or tripping the circuit
    guarded_before = _guarded_calls()
    await redis_cache.cache_delete("sess")
    await redis_cache.bump_version("other")
    assert logger.warning.call_count == 3
    assert not redis_cache._cooling_down()
    assert _guarded_calls() == guarded_before
    fake_cache.fail = False
    await redis_cache.bump_version("other")
    assert fake_cache.store["other"]


def test_row_memo_set_get_drop() -> None:
    clear_row_memo()
    assert memo_get("agent", "abc") is None
    memo_set("agent", "abc", "row")
    assert memo_get("agent", "abc") == "row"
    memo_drop("agent", "abc")
    assert memo_get("agent", "abc") is None
    clear_row_memo()


@pytest.mark.asyncio
async def test_each_redis_client_gets_its_own_bounded_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(events, "_redis_client", None)
    monkeypatch.setattr(events, "_cache_redis_client", None)
    monkeypatch.setattr(events, "_pubsub_redis_client", None)
    monkeypatch.setattr(
        events,
        "get_settings",
        lambda: SimpleNamespace(
            REDIS_URL="redis://example:6379/0",
            REDIS_MAX_CONNECTIONS=40,
            REDIS_COMMAND_WAIT_SECONDS=0.75,
            REDIS_PUBSUB_MAX_CONNECTIONS=6,
            REDIS_CACHE_MAX_CONNECTIONS=12,
            REDIS_SOCKET_CONNECT_TIMEOUT=1.5,
            REDIS_SOCKET_TIMEOUT=4.0,
            REDIS_CACHE_SOCKET_TIMEOUT=0.25,
            REDIS_CACHE_CONNECT_TIMEOUT=0.15,
            REDIS_HEALTH_CHECK_INTERVAL=15,
        ),
    )

    command = await events.get_redis()
    cache = await events.get_cache_redis()
    pubsub = await events.get_pubsub_redis()

    command_pool, cache_pool, pubsub_pool = command.connection_pool, cache.connection_pool, pubsub.connection_pool
    assert len({id(command_pool), id(cache_pool), id(pubsub_pool)}) == 3
    # Only commands wait for a free connection, and only for the configured, shorter-than-socket-timeout bound.
    assert isinstance(command_pool, events._CommandPool)
    assert (command_pool.max_connections, command_pool.timeout) == (40, 0.75)
    assert command_pool.connection_kwargs["socket_timeout"] == 4.0
    assert "retry" not in command_pool.connection_kwargs
    assert isinstance(cache_pool, events._FailFastPool)
    assert cache_pool.max_connections == 12
    assert cache_pool.connection_kwargs["socket_timeout"] == 0.25
    # A dead cache host costs one read budget, not the durable pools' connect timeout.
    assert cache_pool.connection_kwargs["socket_connect_timeout"] == 0.15
    assert command_pool.connection_kwargs["socket_connect_timeout"] == 1.5
    assert cache_pool.connection_kwargs["retry"].get_retries() == 0
    assert isinstance(pubsub_pool, events._FailFastPool)
    assert pubsub_pool.max_connections == 6
    assert pubsub_pool.connection_kwargs["socket_timeout"] == 4.0
    assert await events.get_pubsub_redis() is pubsub
    await events.close_redis()
    assert (events._redis_client, events._cache_redis_client, events._pubsub_redis_client) == (None, None, None)


async def test_a_busy_command_pool_fails_as_overload_and_a_failed_connect_stays_a_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure = AsyncMock()
    monkeypatch.setattr(events._CommandPool, "ensure_connection", ensure)
    pool = events._CommandPool(max_connections=1, timeout=0.0)
    held = await pool.get_connection()

    # When every connection is in use the wait runs out as a pool overload, which is neither of the failures a down
    # Redis shows.
    with pytest.raises(events.RedisPoolOverloadError) as overload:
        await pool.get_connection()
    assert isinstance(overload.value, RedisError)
    assert not isinstance(overload.value, (RedisConnectionError, RedisTimeoutError))

    # Then a freed connection is served again, and a connect that fails is still the library's ConnectionError.
    await pool.release(held)
    held = await pool.get_connection()
    await pool.release(held)
    ensure.side_effect = RedisConnectionError("Error 111 connecting to redis")
    with pytest.raises(RedisConnectionError, match="connecting"):
        await pool.get_connection()
    await pool.disconnect()


async def test_pools_that_never_wait_fail_at_once_as_overload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(events._FailFastPool, "ensure_connection", AsyncMock())
    pool = events._FailFastPool(max_connections=1)
    held = await pool.get_connection()

    with pytest.raises(events.RedisPoolOverloadError):
        await pool.get_connection()

    await pool.release(held)
    await pool.disconnect()


async def test_readiness_probe_treats_a_busy_command_pool_as_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    client = Mock(ping=AsyncMock(side_effect=events.RedisPoolOverloadError("busy")))
    monkeypatch.setattr(events, "_redis_client", client)
    assert await events.ping_redis()

    client.ping.side_effect = RedisConnectionError("refused")
    with pytest.raises(RedisConnectionError):
        await events.ping_redis()


@pytest.mark.parametrize(
    ("failure", "opens_circuit"),
    [(events.RedisPoolOverloadError("busy"), False), (RedisConnectionError("refused"), True)],
)
async def test_only_a_down_redis_opens_the_cache_circuit_not_a_busy_pool(
    monkeypatch: pytest.MonkeyPatch, failure: Exception, opens_circuit: bool
) -> None:
    redis_cache.reset_circuit()
    client = Mock(get=AsyncMock(side_effect=failure), set=AsyncMock(side_effect=failure))
    monkeypatch.setattr(redis_cache, "_client", AsyncMock(return_value=client))

    assert await redis_cache.cache_get("key") is None
    assert redis_cache._cooling_down() is opens_circuit
    redis_cache.reset_circuit()
    assert not await redis_cache.cache_set("key", "value", ttl=10)
    assert redis_cache._cooling_down() is opens_circuit
    redis_cache.reset_circuit()
    assert not await redis_cache.cache_set_nx("key", "value", ttl=10)
    assert redis_cache._cooling_down() is opens_circuit
    redis_cache.reset_circuit()


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def breaker(monkeypatch: pytest.MonkeyPatch) -> tuple[redis_cache.CacheBreaker, _Clock]:
    clock = _Clock()
    circuit = redis_cache.CacheBreaker(cooldown=2.0, clock=clock)
    monkeypatch.setattr(redis_cache, "_breaker", circuit)
    return circuit, clock


def _outage() -> Callable[[], Awaitable[str]]:
    async def down() -> str:
        raise RedisConnectionError("refused")

    return down


def _healthy() -> Callable[[], Awaitable[str]]:
    async def up() -> str:
        return "ok"

    return up


async def test_a_failure_opens_the_breaker_and_refused_calls_never_reach_redis(
    breaker: tuple[redis_cache.CacheBreaker, _Clock],
) -> None:
    calls: list[str] = []

    async def counted() -> str:
        calls.append("call")
        return "ok"

    with pytest.raises(redis_cache.CacheUnavailableError):
        await redis_cache.guarded(_outage())

    for _ in range(3):
        with pytest.raises(redis_cache.CacheUnavailableError, match="circuit open"):
            await redis_cache.guarded(counted)
    assert calls == []


async def test_after_the_cooldown_one_probe_decides_and_success_closes_the_breaker(
    breaker: tuple[redis_cache.CacheBreaker, _Clock],
) -> None:
    _, clock = breaker
    with pytest.raises(redis_cache.CacheUnavailableError):
        await redis_cache.guarded(_outage())
    clock.now += 2.0
    release = asyncio.Event()
    probing = asyncio.Event()
    calls: list[str] = []

    async def probe() -> str:
        calls.append("probe")
        probing.set()
        await release.wait()
        return "ok"

    probe_task = asyncio.create_task(redis_cache.guarded(probe))
    await probing.wait()

    # While the probe is in flight every other caller is still refused.
    with pytest.raises(redis_cache.CacheUnavailableError, match="circuit open"):
        await redis_cache.guarded(_healthy())
    release.set()
    assert await probe_task == "ok"

    # The successful probe closed it: calls flow again.
    assert await redis_cache.guarded(_healthy()) == "ok"
    assert calls == ["probe"]


async def test_a_failed_probe_reopens_the_breaker_for_another_cooldown(
    breaker: tuple[redis_cache.CacheBreaker, _Clock],
) -> None:
    _, clock = breaker
    with pytest.raises(redis_cache.CacheUnavailableError):
        await redis_cache.guarded(_outage())
    clock.now += 2.0

    with pytest.raises(redis_cache.CacheUnavailableError, match="ConnectionError"):
        await redis_cache.guarded(_outage())
    clock.now += 1.9
    with pytest.raises(redis_cache.CacheUnavailableError, match="circuit open"):
        await redis_cache.guarded(_healthy())

    clock.now += 0.1
    assert await redis_cache.guarded(_healthy()) == "ok"


async def test_a_busy_pool_or_a_cancelled_probe_neither_trips_nor_closes_the_breaker(
    breaker: tuple[redis_cache.CacheBreaker, _Clock],
) -> None:
    circuit, clock = breaker

    async def busy() -> str:
        raise events.RedisPoolOverloadError("busy")

    with pytest.raises(redis_cache.CacheUnavailableError):
        await redis_cache.guarded(busy)
    assert not circuit.refusing()

    circuit.trip()
    clock.now += 2.0
    started = asyncio.Event()

    async def stalled() -> str:
        started.set()
        await asyncio.Event().wait()
        return "unreachable"

    probe = asyncio.create_task(redis_cache.guarded(stalled))
    await started.wait()
    assert circuit.refusing()
    _ = probe.cancel()
    with pytest.raises(asyncio.CancelledError):
        await probe

    # The cancelled probe proved nothing, so the next caller becomes the probe.
    assert not circuit.refusing()
    assert await redis_cache.guarded(_healthy()) == "ok"
    assert not circuit.refusing()


def _guarded_calls() -> dict[str, float]:
    family = next(f for f in redis_cache.metric_families() if f.name == "maraclaw_cache_redis_calls_total")
    return {dict(sample.labels)["outcome"]: sample.value for sample in family.samples}


async def test_guarded_counts_ok_refused_pool_busy_and_failed_and_never_a_cancellation(
    breaker: tuple[redis_cache.CacheBreaker, _Clock],
) -> None:
    _, clock = breaker
    before = _guarded_calls()

    async def busy() -> str:
        raise events.RedisPoolOverloadError("busy")

    started = asyncio.Event()

    async def stalled() -> str:
        started.set()
        await asyncio.Event().wait()
        return "unreachable"

    assert await redis_cache.guarded(_healthy()) == "ok"
    with pytest.raises(redis_cache.CacheUnavailableError):
        await redis_cache.guarded(busy)
    with pytest.raises(redis_cache.CacheUnavailableError):
        await redis_cache.guarded(_outage())
    with pytest.raises(redis_cache.CacheUnavailableError, match="circuit open"):
        await redis_cache.guarded(_healthy())
    clock.now += 2.0
    probe = asyncio.create_task(redis_cache.guarded(stalled))
    await started.wait()
    _ = probe.cancel()
    with pytest.raises(asyncio.CancelledError):
        await probe

    after = _guarded_calls()
    assert {outcome: after[outcome] - before[outcome] for outcome in after} == {
        "ok": 1,
        "refused": 1,
        "pool_busy": 1,
        "failed": 1,
    }


async def test_the_breaker_gauge_reads_the_state_without_taking_the_probe_slot(
    breaker: tuple[redis_cache.CacheBreaker, _Clock],
) -> None:
    circuit, clock = breaker

    def gauge() -> float:
        family = next(f for f in redis_cache.metric_families() if f.name == "maraclaw_cache_redis_breaker_open")
        return family.samples[0].value

    assert gauge() == 0
    circuit.trip()
    clock.now += 2.0
    assert [gauge(), gauge()] == [1, 1]
    # Past the cooldown the first real call is still the probe; its success closes the breaker.
    assert await redis_cache.guarded(_healthy()) == "ok"
    assert gauge() == 0


async def test_a_straggler_failure_does_not_extend_an_open_breaker(
    breaker: tuple[redis_cache.CacheBreaker, _Clock],
) -> None:
    circuit, clock = breaker
    first, straggler = circuit.admit(), circuit.admit()
    assert first is not None
    assert straggler is not None
    circuit.failed(first)
    clock.now += 1.5
    circuit.failed(straggler)
    clock.now += 0.5
    assert not circuit.refusing()


async def test_cache_reads_and_fills_are_refused_at_once_while_the_breaker_is_open(
    fake_cache: FakeRedis, breaker: tuple[redis_cache.CacheBreaker, _Clock]
) -> None:
    fake_cache.fail = True
    assert await redis_cache.cache_get("k") is None
    fake_cache.fail = False
    fake_cache.store["k"] = "v"

    assert await redis_cache.cache_get("k") is None
    assert await redis_cache.read_version("k") is None
    assert not await redis_cache.cache_set("k", "new", ttl=10)
    assert not await redis_cache.cache_set_nx("n", "new", ttl=10)
    assert fake_cache.store == {"k": "v"}

    breaker[1].now += 2.0
    assert await redis_cache.cache_get("k") == "v"


async def test_a_single_flight_load_is_shared_by_concurrent_callers() -> None:
    flights: SingleFlight[str, int] = SingleFlight()
    release = asyncio.Event()
    loads: list[str] = []

    async def load() -> int:
        loads.append("load")
        await release.wait()
        return 7

    first = asyncio.create_task(flights.run("key", load))
    second = asyncio.create_task(flights.run("key", load))
    other = asyncio.create_task(flights.run("other", load))
    await asyncio.sleep(0)
    release.set()

    assert await asyncio.gather(first, second, other) == [7, 7, 7]
    assert loads == ["load", "load"]


async def test_a_failed_single_flight_load_reaches_every_waiter_and_the_next_attempt_retries() -> None:
    flights: SingleFlight[str, str] = SingleFlight()
    release = asyncio.Event()
    attempts: list[str] = []

    async def broken() -> str:
        attempts.append("broken")
        await release.wait()
        raise ValueError("load failed")

    async def healthy() -> str:
        attempts.append("healthy")
        return "fresh"

    waiters = [asyncio.create_task(flights.run("key", broken)) for _ in range(3)]
    await asyncio.sleep(0)
    release.set()
    outcomes = await asyncio.gather(*waiters, return_exceptions=True)

    assert [type(outcome) for outcome in outcomes] == [ValueError] * 3
    assert await flights.run("key", healthy) == "fresh"
    assert attempts == ["broken", "healthy"]


async def test_cancelling_one_waiter_does_not_cancel_the_shared_load() -> None:
    flights: SingleFlight[str, str] = SingleFlight()
    release = asyncio.Event()
    loads: list[str] = []

    async def load() -> str:
        loads.append("load")
        await release.wait()
        return "loaded"

    leader = asyncio.create_task(flights.run("key", load))
    follower = asyncio.create_task(flights.run("key", load))
    await asyncio.sleep(0)

    _ = leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader
    release.set()

    assert await follower == "loaded"
    assert loads == ["load"]


def test_the_cache_connect_timeout_may_not_exceed_the_cache_read_budget() -> None:
    assert Settings(REDIS_CACHE_CONNECT_TIMEOUT=0.2, REDIS_CACHE_SOCKET_TIMEOUT=0.2).REDIS_CACHE_CONNECT_TIMEOUT == 0.2
    with pytest.raises(ValidationError, match="REDIS_CACHE_CONNECT_TIMEOUT"):
        Settings(REDIS_CACHE_CONNECT_TIMEOUT=0.3, REDIS_CACHE_SOCKET_TIMEOUT=0.2)
