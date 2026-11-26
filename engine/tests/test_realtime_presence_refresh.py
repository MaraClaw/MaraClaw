"""Presence TTL refresh, pipelined presence lookup and the websocket refresh task."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Self
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import anyio
import pytest
from fastapi import WebSocket
from redis.exceptions import TimeoutError as RedisTimeoutError
from starlette.types import Message

from app.api import websocket
from app.records.agent import AgentRecord
from app.records.user import UserRecord
from app.services import chat_tickets
from app.services.realtime_runtime import router
from app.services.realtime_runtime.router import PRESENCE_REFRESH_SECONDS, PRESENCE_TTL_SECONDS


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakePipeline:
    def __init__(self, store: FakeRedis) -> None:
        self.store = store
        self.queued: list[Callable[[], object]] = []

    async def __aenter__(self) -> Self:
        self.store.pipelines += 1
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    def sadd(self, key: str, member: str) -> Self:
        self.queued.append(lambda: self.store.sadd(key, member))
        return self

    def hset(self, key: str, field: str, value: str) -> Self:
        self.queued.append(lambda: self.store.hset(key, field, value))
        return self

    def expire(self, key: str, seconds: int) -> Self:
        self.queued.append(lambda: self.store.expire(key, seconds))
        return self

    def hgetall(self, key: str) -> Self:
        self.store.hgetall_queued += 1
        self.queued.append(lambda: self.store.read_hash(key))
        return self

    def srem(self, key: str, *members: str) -> Self:
        self.queued.append(lambda: self.store.srem_now(key, *members))
        return self

    def delete(self, key: str) -> Self:
        self.queued.append(lambda: self.store.delete_now(key))
        return self

    def zadd(self, key: str, mapping: dict[str, float]) -> Self:
        self.queued.append(lambda: self.store.zadd_now(key, mapping))
        return self

    def zrem(self, key: str, *members: str) -> Self:
        self.queued.append(lambda: self.store.zrem_now(key, *members))
        return self

    def zremrangebyscore(self, key: str, minimum: str, maximum: float) -> Self:
        self.queued.append(lambda: self.store.zremrangebyscore_now(key, minimum, maximum))
        return self

    def zrangebyscore(self, key: str, minimum: str, maximum: str) -> Self:
        self.queued.append(lambda: self.store.zrangebyscore_now(key, minimum, maximum))
        return self

    def zcard(self, key: str) -> Self:
        self.queued.append(lambda: self.store.zcard_now(key))
        return self

    def get(self, key: str) -> Self:
        self.queued.append(lambda: self.store.values.get(key))
        return self

    async def execute(self) -> list[object]:
        return [operation() for operation in self.queued]


class FakeRedis:
    """Just the commands the realtime router uses, with TTLs driven by an injected clock."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.sets: dict[str, set[str]] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.values: dict[str, str] = {}
        self.deadlines: dict[str, float] = {}
        self.channels: dict[str, Callable[[str], Awaitable[object]]] = {}
        self.pipelines = 0
        self.direct_hgetall = 0
        self.hgetall_queued = 0

    def _expire_due(self) -> None:
        for key, deadline in list(self.deadlines.items()):
            if deadline <= self.clock.now:
                del self.deadlines[key]
                _ = self.sets.pop(key, None)
                _ = self.hashes.pop(key, None)
                _ = self.zsets.pop(key, None)

    def sadd(self, key: str, member: str) -> int:
        self._expire_due()
        members = self.sets.setdefault(key, set())
        added = member not in members
        members.add(member)
        return int(added)

    def hset(self, key: str, field: str, value: str) -> int:
        self._expire_due()
        self.hashes.setdefault(key, {})[field] = value
        return 1

    def expire(self, key: str, seconds: int) -> bool:
        self._expire_due()
        if key not in self.sets and key not in self.hashes and key not in self.zsets:
            return False
        self.deadlines[key] = self.clock.now + seconds
        return True

    def zadd_now(self, key: str, mapping: dict[str, float]) -> int:
        self._expire_due()
        scores = self.zsets.setdefault(key, {})
        added = len(mapping.keys() - scores.keys())
        scores.update(mapping)
        return added

    def zrem_now(self, key: str, *members: str) -> int:
        self._expire_due()
        scores = self.zsets.get(key, {})
        removed = len(scores.keys() & set(members))
        for member in members:
            _ = scores.pop(member, None)
        return removed

    @staticmethod
    def _within(score: float, minimum: str, maximum: str) -> bool:
        lower = float("-inf") if minimum == "-inf" else float(minimum.removeprefix("("))
        upper = float("inf") if maximum == "+inf" else float(maximum.removeprefix("("))
        return (score > lower if minimum.startswith("(") else score >= lower) and (
            score < upper if maximum.startswith("(") else score <= upper
        )

    def zremrangebyscore_now(self, key: str, minimum: str, maximum: float) -> int:
        self._expire_due()
        scores = self.zsets.get(key, {})
        due = [member for member, score in scores.items() if self._within(score, minimum, str(maximum))]
        for member in due:
            del scores[member]
        return len(due)

    def zcard_now(self, key: str) -> int:
        self._expire_due()
        return len(self.zsets.get(key, {}))

    def zrangebyscore_now(self, key: str, minimum: str, maximum: str) -> list[str]:
        self._expire_due()
        scores = self.zsets.get(key, {})
        return sorted((m for m, s in scores.items() if self._within(s, minimum, maximum)), key=scores.__getitem__)

    def read_hash(self, key: str) -> dict[str, str]:
        self._expire_due()
        return dict(self.hashes.get(key, {}))

    def srem_now(self, key: str, *members: str) -> int:
        self._expire_due()
        live = self.sets.get(key, set())
        removed = len(live.intersection(members))
        live.difference_update(members)
        return removed

    def delete_now(self, key: str) -> int:
        self._expire_due()
        return int(self.hashes.pop(key, None) is not None)

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        _ = transaction
        return FakePipeline(self)

    async def smembers(self, key: str) -> set[str]:
        self._expire_due()
        return set(self.sets.get(key, set()))

    async def hgetall(self, key: str) -> dict[str, str]:
        self.direct_hgetall += 1
        return self.read_hash(key)

    async def srem(self, key: str, *members: str) -> int:
        return self.srem_now(key, *members)

    async def publish(self, channel: str, payload: str) -> int:
        handler = self.channels.get(channel)
        if handler is None:
            return 0
        _ = await handler(payload)
        return 1


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch, clock: Clock) -> FakeRedis:
    fake = FakeRedis(clock)
    monkeypatch.setattr(router, "get_redis", AsyncMock(return_value=fake))
    monkeypatch.setattr(router, "_wall_clock", lambda: clock.now)
    return fake


def open_socket(frames: list[Message]) -> WebSocket:
    async def receive() -> Message:
        return {"type": "websocket.connect"}

    async def send(message: Message) -> None:
        frames.append(message)

    return WebSocket({"type": "websocket", "headers": []}, receive, send)


@pytest.mark.parametrize("refreshed", [True, False])
async def test_connection_older_than_presence_ttl_receives_cross_instance_done_only_when_refreshed(
    redis: FakeRedis, clock: Clock, refreshed: bool
) -> None:
    # Given a socket registered on instance B and a router on instance A.
    agent_id, session_id, user_id = str(uuid4()), str(uuid4()), str(uuid4())
    frames: list[Message] = []
    socket = open_socket(frames)
    await socket.accept()
    frames.clear()
    router_a, router_b = router.RealtimeRouter(), router.RealtimeRouter()
    router_a.instance_id, router_b.instance_id = "a", "b"
    manager_b = websocket.ConnectionManager()
    manager_b.active_connections[agent_id] = [(socket, session_id, user_id)]
    redis.channels["realtime:ws:instance:b"] = lambda payload: router.RealtimeRouter._deliver_message(
        payload, manager_b.deliver_pubsub_message
    )
    _ = await router_b.register_connection(agent_id=agent_id, websocket=socket, session_id=session_id, user_id=user_id)

    # When the socket outlives the presence TTL, with or without the 60 s refresh in between.
    clock.advance(PRESENCE_TTL_SECONDS - 80)
    if refreshed:
        await router_b.refresh_connection(agent_id=agent_id, websocket=socket)
    clock.advance(PRESENCE_TTL_SECONDS - 80)
    await router_a.route_message(
        agent_id=agent_id, message={"type": "done"}, local_connections=[], session_id=session_id
    )

    # Then only the refreshed connection is still discoverable and receives the event.
    assert 2 * (PRESENCE_TTL_SECONDS - 80) > PRESENCE_TTL_SECONDS
    assert len(frames) == (1 if refreshed else 0)


@pytest.mark.parametrize("lost", ["both", "hash"])
async def test_refresh_restores_expired_presence_without_duplicate_index_members(
    redis: FakeRedis, clock: Clock, lost: str
) -> None:
    agent_id = str(uuid4())
    socket = open_socket([])
    instance = router.RealtimeRouter()
    connection_id = await instance.register_connection(
        agent_id=agent_id, websocket=socket, session_id="session", user_id="user"
    )
    if lost == "both":
        clock.advance(PRESENCE_TTL_SECONDS + 1)
    else:
        _ = redis.hashes.pop(f"realtime:ws:conn:{connection_id}")

    await instance.refresh_connection(agent_id=agent_id, websocket=socket)

    assert redis.sets[f"realtime:ws:agent:{agent_id}"] == {connection_id}
    records = await instance._list_presence(agent_id)
    assert [(record.get("session_id"), record.get("user_id")) for record in records] == [("session", "user")]
    assert len(redis.zsets[f"realtime:ws:session:{agent_id}:session"]) == 1


async def test_refresh_without_registration_does_not_touch_redis(redis: FakeRedis) -> None:
    await router.RealtimeRouter().refresh_connection(agent_id="agent", websocket=open_socket([]))

    assert redis.pipelines == 0


async def test_presence_listing_reads_every_connection_in_one_pipeline(redis: FakeRedis) -> None:
    agent_id = str(uuid4())
    instance = router.RealtimeRouter()
    sockets = [open_socket([]) for _ in range(3)]
    for index, socket in enumerate(sockets):
        _ = await instance.register_connection(
            agent_id=agent_id, websocket=socket, session_id=f"session-{index}", user_id="user"
        )
    stale = f"realtime:ws:conn:{sockets[0].state.realtime_connection_id}"
    _ = redis.hashes.pop(stale)
    redis.pipelines = 0

    records = await instance._list_presence(agent_id)

    assert sorted(record.get("session_id", "") for record in records) == ["session-1", "session-2"]
    assert redis.pipelines == 1
    assert redis.direct_hgetall == 0
    assert sockets[0].state.realtime_connection_id not in redis.sets[f"realtime:ws:agent:{agent_id}"]


async def watch(
    instance: router.RealtimeRouter, agent_id: str, session_id: str, user_id: str, *, instance_id: str
) -> WebSocket:
    """Register one socket of ``user_id`` on ``session_id``, as it would be on the replica ``instance_id``."""
    socket = open_socket([])
    instance.instance_id = instance_id
    _ = await instance.register_connection(agent_id=agent_id, websocket=socket, session_id=session_id, user_id=user_id)
    return socket


async def remote_instances(agent_id: str, session_id: str, user_id: str | None = None) -> dict[str, int]:
    """What a replica that holds no socket of its own learns about a session."""
    reader = router.RealtimeRouter()
    reader.instance_id = "reader"
    return (await reader._session_lookup(agent_id, session_id, user_id, False)).remote_instances


async def test_session_lookup_reads_one_pipeline_and_no_connection_hash(redis: FakeRedis) -> None:
    # Given many sockets of the agent on other sessions, and two on the session of interest.
    agent_id, session_id = str(uuid4()), str(uuid4())
    instance = router.RealtimeRouter()
    for index in range(20):
        _ = await watch(instance, agent_id, f"other-{index}", "user", instance_id=f"replica-{index}")
    _ = await watch(instance, agent_id, session_id, "owner", instance_id="replica-a")
    _ = await watch(instance, agent_id, session_id, "viewer", instance_id="replica-b")
    redis.pipelines = redis.hgetall_queued = 0

    # When another replica asks who watches the session.
    found = await remote_instances(agent_id, session_id)

    # Then the answer comes from one pipeline that reads no connection hash, and a user filter narrows it.
    assert found == {"replica-a": 1, "replica-b": 1}
    assert (redis.pipelines, redis.hgetall_queued, redis.direct_hgetall) == (1, 0, 0)
    assert await remote_instances(agent_id, session_id, "owner") == {"replica-a": 1}
    assert await remote_instances(agent_id, "nobody-watches-this") == {}


async def test_session_lookup_skips_this_replica_and_returns_the_acl_version_in_the_same_round_trip(
    redis: FakeRedis,
) -> None:
    agent_id, session_id = str(uuid4()), str(uuid4())
    instance = router.RealtimeRouter()
    _ = await watch(instance, agent_id, session_id, "owner", instance_id="reader")
    _ = await watch(instance, agent_id, session_id, "owner", instance_id="replica-a")
    redis.values[f"aclver:{agent_id}"] = "v7"
    redis.pipelines = 0
    reader = router.RealtimeRouter()
    reader.instance_id = "reader"

    lookup = await reader._session_lookup(agent_id, session_id, None, True)

    assert (lookup.remote_instances, lookup.acl_version, redis.pipelines) == ({"replica-a": 1}, "v7", 1)
    redis.pipelines = 0
    assert (await reader._session_lookup(agent_id, session_id, None, False)).acl_version is None
    assert redis.pipelines == 1


async def test_session_entries_expire_with_their_own_ttl_even_while_neighbours_keep_the_index_alive(
    redis: FakeRedis, clock: Clock
) -> None:
    # Given two replicas watching a session; only one keeps refreshing, the other died without unregistering.
    agent_id, session_id = str(uuid4()), str(uuid4())
    instance = router.RealtimeRouter()
    _ = await watch(instance, agent_id, session_id, "user", instance_id="dead")
    survivor = await watch(instance, agent_id, session_id, "user", instance_id="alive")
    for _ in range(4):
        clock.advance(PRESENCE_REFRESH_SECONDS)
        await instance.refresh_connection(agent_id=agent_id, websocket=survivor)

    # When the dead replica's TTL has passed, the survivor's refreshes keep the shared index key alive.
    assert 4 * PRESENCE_REFRESH_SECONDS > PRESENCE_TTL_SECONDS
    found = await remote_instances(agent_id, session_id)

    # Then the dead replica's entry is no longer served.
    assert found == {"alive": 1}
    # And once nobody refreshes, the whole index is gone within one TTL.
    clock.advance(PRESENCE_TTL_SECONDS + 1)
    assert await remote_instances(agent_id, session_id) == {}
    assert not redis.zsets


@pytest.mark.parametrize("path", ["disconnect", "eviction"])
async def test_unregistering_removes_every_record_of_the_connection(redis: FakeRedis, path: str) -> None:
    agent_id, session_id = str(uuid4()), str(uuid4())
    instance = router.RealtimeRouter()
    kept = await watch(instance, agent_id, session_id, "user", instance_id="kept")
    gone = await watch(instance, agent_id, session_id, "user", instance_id="gone")
    registered = router.RealtimeRouter.registered(gone)
    assert registered is not None

    if path == "disconnect":
        await instance.unregister_connection(agent_id=agent_id, websocket=gone)
    else:
        # An evicted socket no longer carries its state; the withdraw path hands the presence over.
        await instance.unregister_connection_id(agent_id=agent_id, connection_id=registered[0], presence=registered[1])

    assert await remote_instances(agent_id, session_id) == {"kept": 1}
    assert router.RealtimeRouter.registered(kept) is not None
    assert len(redis.hashes) == 1
    assert len(redis.sets[f"realtime:ws:agent:{agent_id}"]) == 1
    assert len(redis.zsets[f"realtime:ws:session:{agent_id}:{session_id}"]) == 1


async def test_viewing_and_active_session_answers_follow_registration_unregistration_and_expiry(
    redis: FakeRedis, clock: Clock
) -> None:
    agent_id, session_id = str(uuid4()), str(uuid4())
    instance = router.RealtimeRouter()

    async def answers() -> tuple[bool, bool, list[str]]:
        return (
            await instance.is_user_viewing_session(agent_id=agent_id, session_id=session_id, user_id="user"),
            await instance.is_user_viewing_session(agent_id=agent_id, session_id=session_id, user_id="someone"),
            await instance.get_active_session_ids(agent_id),
        )

    assert await answers() == (False, False, [])
    socket = await watch(instance, agent_id, session_id, "user", instance_id="replica")
    assert await answers() == (True, False, [session_id])
    await instance.unregister_connection(agent_id=agent_id, websocket=socket)
    assert await answers() == (False, False, [])
    _ = await watch(instance, agent_id, session_id, "user", instance_id="replica")
    clock.advance(PRESENCE_TTL_SECONDS + 1)
    assert await answers() == (False, False, [])
    assert redis.direct_hgetall == 0


class RefreshTicker:
    """Lets the first sleep elapse, then parks forever and records cancellation."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.intervals: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.intervals.append(seconds)
        if len(self.intervals) == 1:
            return
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.events.append("refresh-cancelled")
            raise


def parked_socket(release: asyncio.Event) -> WebSocket:
    calls = 0

    async def receive() -> Message:
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"type": "websocket.connect"}
        await release.wait()
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(_message: Message) -> None:
        return None

    return WebSocket({"type": "websocket", "headers": []}, receive, send)


def closable_socket(closes: list[int]) -> WebSocket:
    """Parks on receive until the server closes it, like an ASGI server delivering the disconnect."""
    release = asyncio.Event()
    calls = 0

    async def receive() -> Message:
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"type": "websocket.connect"}
        await release.wait()
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(message: Message) -> None:
        if message["type"] == "websocket.close":
            closes.append(message["code"])
            release.set()

    return WebSocket({"type": "websocket", "headers": []}, receive, send)


@dataclass
class HandlerEnv:
    binding: chat_tickets.ChatTicketBinding
    events: list[str]
    refreshed: asyncio.Event
    refresh: AsyncMock


@pytest.fixture
def handler_env(monkeypatch: pytest.MonkeyPatch) -> HandlerEnv:
    binding = chat_tickets.ChatTicketBinding(
        user_id=uuid4(),
        tenant_id=uuid4(),
        agent_id=uuid4(),
        session_id=uuid4(),
        auth_deadline=int(time.time()) + 3600,
        auth_generation=0,
    )
    user = UserRecord(id=binding.user_id, tenant_id=binding.tenant_id, display_name="Member")
    agent = AgentRecord(id=binding.agent_id, creator_id=user.id, name="Agent")
    events: list[str] = []
    refreshed = asyncio.Event()

    async def record_refresh(**_kwargs: object) -> None:
        events.append("refresh")
        refreshed.set()

    async def record_unregister(**_kwargs: object) -> None:
        events.append("unregister")

    refresh = AsyncMock(side_effect=record_refresh)
    stub = SimpleNamespace(
        register_connection=AsyncMock(return_value="connection"),
        unregister_connection=AsyncMock(side_effect=record_unregister),
        refresh_connection=refresh,
    )
    monkeypatch.setattr(websocket, "manager", websocket.ConnectionManager())
    monkeypatch.setattr(websocket, "realtime_router", stub)
    monkeypatch.setattr(chat_tickets, "redeem", AsyncMock(return_value=binding))
    monkeypatch.setattr(chat_tickets, "authorize", AsyncMock(return_value=(user, agent, "use")))
    monkeypatch.setattr(chat_tickets, "authorized_session", AsyncMock())
    monkeypatch.setattr(websocket.chat_message_dao, "list_recent", AsyncMock(return_value=[]))
    return HandlerEnv(binding, events, refreshed, refresh)


async def test_refresh_task_runs_while_open_and_is_cancelled_before_unregister(
    monkeypatch: pytest.MonkeyPatch, handler_env: HandlerEnv
) -> None:
    binding = handler_env.binding
    ticker = RefreshTicker(handler_env.events)
    monkeypatch.setattr(websocket, "_presence_sleep", ticker.sleep)
    release = asyncio.Event()
    handler = websocket.WebSocketChatHandler(
        parked_socket(release), binding.agent_id, "opaque", str(binding.session_id)
    )

    run = asyncio.create_task(handler.run())
    async with asyncio.timeout(5):
        _ = await handler_env.refreshed.wait()
    task = handler._presence_task
    assert task is not None
    assert not task.done()
    release.set()
    async with asyncio.timeout(5):
        await run

    assert ticker.intervals[0] == PRESENCE_REFRESH_SECONDS
    assert handler_env.events == ["refresh", "refresh-cancelled", "unregister"]
    assert task.cancelled()
    assert handler._presence_task is None
    handler_env.refresh.assert_awaited_once()
    assert handler_env.refresh.await_args is not None
    assert handler_env.refresh.await_args.kwargs["agent_id"] == str(binding.agent_id)


async def test_presence_refresh_redis_failure_is_logged_and_never_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    report = Mock()
    warning = Mock()
    monkeypatch.setattr(websocket, "report_exception", report)
    monkeypatch.setattr(websocket.logger, "warning", warning)
    monkeypatch.setattr(
        websocket,
        "realtime_router",
        SimpleNamespace(refresh_connection=AsyncMock(side_effect=RedisTimeoutError("presence refresh timeout"))),
    )

    await websocket.ConnectionManager().refresh_presence("agent", open_socket([]))

    report.assert_called_once()
    warning.assert_called_once()


@pytest.mark.asyncio
async def test_refresh_loop_survives_unexpected_failure_on_normal_cadence(
    monkeypatch: pytest.MonkeyPatch, handler_env: HandlerEnv
) -> None:
    binding = handler_env.binding
    handler = websocket.WebSocketChatHandler(open_socket([]), binding.agent_id, "opaque", str(binding.session_id))
    intervals: asyncio.Queue[float] = asyncio.Queue()
    ticks: asyncio.Queue[None] = asyncio.Queue()
    cancelled = asyncio.Event()
    handler_env.refresh.side_effect = [RuntimeError("refresh failed"), None]

    async def sleep(seconds: float) -> None:
        await intervals.put(seconds)
        await ticks.get()

    async def refresh() -> None:
        try:
            await handler._refresh_presence()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(websocket, "_presence_sleep", sleep)
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(refresh)
        with anyio.fail_after(5):
            for _ in range(2):
                assert await intervals.get() == PRESENCE_REFRESH_SECONDS
                await ticks.put(None)
            assert await intervals.get() == PRESENCE_REFRESH_SECONDS
        assert handler_env.refresh.await_count == 2
        tasks.cancel_scope.cancel()

    assert cancelled.is_set()


async def test_resubscribe_close_ends_the_handler_with_a_reconnectable_code_and_releases_presence(
    monkeypatch: pytest.MonkeyPatch, handler_env: HandlerEnv
) -> None:
    binding = handler_env.binding
    monkeypatch.setattr(websocket, "_presence_sleep", RefreshTicker(handler_env.events).sleep)
    closes: list[int] = []
    handler = websocket.WebSocketChatHandler(
        closable_socket(closes), binding.agent_id, "opaque", str(binding.session_id)
    )

    run = asyncio.create_task(handler.run())
    async with asyncio.timeout(5):
        _ = await handler_env.refreshed.wait()
    await websocket.manager.close_local_connections()
    async with asyncio.timeout(5):
        await run

    assert closes == [websocket.RESUBSCRIBE_CLOSE_CODE]
    assert closes[0] not in (4001, 4003)
    assert handler_env.events == ["refresh", "refresh-cancelled", "unregister"]
    assert websocket.manager.active_connections[str(binding.agent_id)] == []


async def test_user_socket_cap_counts_every_instance_and_frees_slots_on_close_or_expiry(
    redis: FakeRedis, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a cap of two, with the user's sockets registered through two different processes.
    monkeypatch.setattr(router.settings, "CHAT_SOCKETS_PER_USER", 2)
    agent_id, session_id, user_id = str(uuid4()), str(uuid4()), str(uuid4())
    router_a, router_b = router.RealtimeRouter(), router.RealtimeRouter()
    router_a.instance_id, router_b.instance_id = "a", "b"
    first = await watch(router_a, agent_id, session_id, user_id, instance_id="a")
    _ = await watch(router_b, agent_id, session_id, user_id, instance_id="b")

    # When a third socket arrives on either process, and another user's socket arrives.
    for instance in (router_a, router_b):
        with pytest.raises(router.UserSocketLimitError):
            _ = await watch(instance, agent_id, session_id, user_id, instance_id=instance.instance_id)
    _ = await watch(router_a, agent_id, session_id, str(uuid4()), instance_id="a")

    # Then it is refused and leaves no presence; the other user is unaffected.
    assert len(redis.zsets[f"realtime:ws:user:{user_id}"]) == 2
    assert len(redis.sets[f"realtime:ws:agent:{agent_id}"]) == 3
    # And closing a socket frees its slot at once.
    await router_a.unregister_connection(agent_id=agent_id, websocket=first)
    _ = await watch(router_b, agent_id, session_id, user_id, instance_id="b")
    with pytest.raises(router.UserSocketLimitError):
        _ = await watch(router_b, agent_id, session_id, user_id, instance_id="b")
    # And sockets of a dead process stop counting once their presence expires, without any unregistration.
    clock.advance(PRESENCE_TTL_SECONDS + 1)
    _ = await watch(router_a, agent_id, session_id, user_id, instance_id="a")
