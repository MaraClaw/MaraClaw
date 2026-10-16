import asyncio
import inspect
import json
from typing import override

import pytest
from redis.asyncio import ConnectionPool, Redis
from redis.asyncio.connection import Connection
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import ConnectionError

from app.services.realtime_runtime import router


class MemoryConnection(Connection):
    def __init__(self) -> None:
        super().__init__(retry=Retry(NoBackoff(), 1))
        self.connected = False
        self.messages: asyncio.Queue[object] = asyncio.Queue()
        self.commands: asyncio.Queue[tuple[object, ...]] = asyncio.Queue()
        self.reconnecting = asyncio.Event()
        self.allow_connect = asyncio.Event()
        self.allow_connect.set()

    @property
    @override
    def is_connected(self) -> bool:
        return self.connected

    @override
    async def connect(self) -> None:
        if self.connected:
            return
        self.reconnecting.set()
        await self.allow_connect.wait()
        self.connected = True
        for reference in self._connect_callbacks:
            callback = reference()
            if callback:
                result = callback(self)
                if inspect.isawaitable(result):
                    await result

    @override
    async def disconnect(
        self,
        nowait: bool = False,
        error: Exception | None = None,
        failure_count: int | None = None,
        health_check_failed: bool = False,
    ) -> None:
        self.connected = False

    @override
    async def can_read_destructive(self) -> bool:
        return False

    @override
    async def can_read(self) -> bool:
        return False

    @override
    async def send_command(self, *args: object, **_kwargs: object) -> None:
        await self.commands.put(args)

    @override
    async def read_response(self, *args: object, **kwargs: object) -> object:
        response = await self.messages.get()
        if isinstance(response, BaseException):
            raise response
        return response


async def next_command(connection: MemoryConnection) -> tuple[object, ...]:
    return await asyncio.wait_for(connection.commands.get(), 1)


async def settled_ack(connection: MemoryConnection, channel: str) -> None:
    await connection.messages.put([b"subscribe", channel.encode(), 1])
    await connection.messages.put([b"message", channel.encode(), json.dumps({"agent_id": "barrier", "message": {}})])


async def test_ack_loss_transparent_retry_reack_and_repeated_start(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = ConnectionPool()
    connection = MemoryConnection()
    monkeypatch.setattr(pool, "make_connection", lambda: connection)
    redis = Redis(connection_pool=pool)

    async def existing() -> Redis:
        return redis

    monkeypatch.setattr(router, "get_redis", existing)
    instance = router.RealtimeRouter()
    delivered = asyncio.Event()

    async def deliver(**_kwargs: object) -> None:
        delivered.set()

    try:
        for _ in range(2):
            delivered.clear()
            await instance.start(deliver)
            assert not instance.ready
            assert (await next_command(connection))[0] == "SUBSCRIBE"
            assert not instance.ready
            await settled_ack(connection, instance._instance_channel())
            await asyncio.wait_for(delivered.wait(), 1)
            assert instance.ready
            delivered.clear()
            connection.reconnecting.clear()
            connection.allow_connect.clear()
            await connection.messages.put(ConnectionError("synthetic link loss"))
            await asyncio.wait_for(connection.reconnecting.wait(), 1)
            assert not instance.ready
            connection.allow_connect.set()
            assert (await next_command(connection))[0] == "SUBSCRIBE"
            assert not instance.ready
            await settled_ack(connection, instance._instance_channel())
            await asyncio.wait_for(delivered.wait(), 1)
            assert instance.ready
            await instance.stop()
            assert not instance.ready
            assert not instance._subscriber_task
    finally:
        await instance.stop()
        await redis.aclose()
        await pool.aclose()


async def test_distinct_replicas_route_presence_to_remote_channel_only(monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = router.RealtimeRouter(), router.RealtimeRouter()
    first.instance_id, second.instance_id = "replica-a", "replica-b"
    delivered: list[dict[str, object]] = []
    remote: list[tuple[str, str]] = []
    pool = ConnectionPool()
    connection = MemoryConnection()
    monkeypatch.setattr(pool, "make_connection", lambda: connection)
    received = asyncio.Event()

    class Store:
        connection_pool = pool

        async def smembers(self, _key: str) -> set[str]:
            return {"a", "b"}

        async def hgetall(self, key: str) -> dict[str, str]:
            identity = "replica-a" if key.endswith(":a") else "replica-b"
            return {"instance_id": identity, "session_id": "session", "user_id": "user"}

        async def publish(self, channel: str, payload: str) -> int:
            remote.append((channel, payload))
            if channel == second._instance_channel():
                await connection.messages.put([b"message", channel.encode(), payload])
            return 1

    async def existing() -> Store:
        return Store()

    async def deliver(**kwargs: object) -> None:
        delivered.append(kwargs)
        received.set()

    monkeypatch.setattr(router, "get_redis", existing)
    assert first._instance_channel() != second._instance_channel()
    assert {record["instance_id"] for record in await first._list_presence("agent")} == {"replica-a", "replica-b"}
    await second.start(deliver)
    try:
        assert (await next_command(connection))[0] == "SUBSCRIBE"
        await settled_ack(connection, second._instance_channel())
        await asyncio.wait_for(received.wait(), 1)
        assert second.ready
        delivered.clear()
        received.clear()
        await first.route_message(
            agent_id="agent", message={"text": "hello"}, local_connections=[], session_id="session", user_id="user"
        )
        await asyncio.wait_for(received.wait(), 1)
    finally:
        await second.stop()
        await pool.aclose()
    assert [channel for channel, _payload in remote] == [second._instance_channel()]
    assert delivered == [
        {"agent_id": "agent", "payload": {"text": "hello"}, "session_id": "session", "user_id": "user"}
    ]
