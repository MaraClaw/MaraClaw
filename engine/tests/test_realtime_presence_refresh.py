"""Presence TTL refresh, pipelined presence lookup and the websocket refresh task."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Self
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

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
        self.queued.append(lambda: self.store.read_hash(key))
        return self

    def srem(self, key: str, *members: str) -> Self:
        self.queued.append(lambda: self.store.srem_now(key, *members))
        return self

    def delete(self, key: str) -> Self:
        self.queued.append(lambda: self.store.delete_now(key))
        return self

    async def execute(self) -> list[object]:
        return [operation() for operation in self.queued]


class FakeRedis:
    """Just the commands the realtime router uses, with TTLs driven by an injected clock."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.sets: dict[str, set[str]] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.deadlines: dict[str, float] = {}
        self.channels: dict[str, Callable[[str], Awaitable[object]]] = {}
        self.pipelines = 0
        self.direct_hgetall = 0

    def _expire_due(self) -> None:
        for key, deadline in list(self.deadlines.items()):
            if deadline <= self.clock.now:
                del self.deadlines[key]
                _ = self.sets.pop(key, None)
                _ = self.hashes.pop(key, None)

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
        if key not in self.sets and key not in self.hashes:
            return False
        self.deadlines[key] = self.clock.now + seconds
        return True

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
