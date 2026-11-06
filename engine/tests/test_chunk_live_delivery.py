"""Chunk live delivery through the real publish_chunk, ConnectionManager, RealtimeRouter and delivery authorization."""

import asyncio
import json
import time
from contextlib import suppress
from typing import override
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException, WebSocket
from starlette.types import Message

from app.api import websocket
from app.core import access_cache
from app.records.gateway_message import GatewayMessageRecord
from app.services import (
    chat_tickets,
    openclaw_gateway_delivery as delivery,
    openclaw_gateway_rpc as rpc,
    openclaw_gateway_stream as streaming,
)
from app.services.realtime_runtime import router as realtime
from app.services.shared_chat import shared_session_id
from test_openclaw_gateway_coalescing import IngestProbe, delta_line
from test_openclaw_gateway_rpc import _Turn, wire_terminal
from test_websocket_session_query import MemoryPresence

# Healthy but slower than the one-second publication deadline this suite guards against regressing to.
SLOW = 1.3


class Link:
    """A fake socket transport that records frames and can stall every text send."""

    def __init__(self, *, stalled: bool = False) -> None:
        self.frames: list[Message] = []
        self.stalled = stalled
        self.send_cancelled = False
        self.socket = WebSocket({"type": "websocket", "headers": []}, self._receive, self._send)

    async def _receive(self) -> Message:
        return {"type": "websocket.connect"}

    async def _send(self, message: Message) -> None:
        if self.stalled and message["type"] == "websocket.send":
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.send_cancelled = True
                raise
        self.frames.append(message)

    def seqs(self) -> list[int]:
        return [json.loads(str(frame["text"]))["seq"] for frame in self.frames]


class SlowPresence(MemoryPresence):
    @override
    async def smembers(self, key: str) -> set[str]:
        await asyncio.sleep(SLOW)
        return await super().smembers(key)


class GatedPresence(MemoryPresence):
    """Holds the first presence read, the Redis round trip every routed chunk pays, until released."""

    def __init__(self, agent_id: str, records: dict[str, dict[str, str]]) -> None:
        super().__init__(agent_id, records)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.reads = 0

    @override
    async def smembers(self, key: str) -> set[str]:
        self.reads += 1
        if self.reads == 1:
            self.entered.set()
            await self.release.wait()
        return await super().smembers(key)


class Pipeline:
    """publish_chunk -> manager -> router -> authorize_chat_delivery, with fake sockets, Redis and ticket authorization."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, shared: bool) -> None:
        self.agent_id, self.user_id = uuid4(), uuid4()
        self.shared = shared
        self.session_id = shared_session_id(self.agent_id) if shared else uuid4()
        self.message = GatewayMessageRecord(
            uuid4(), self.agent_id, "hi", conversation_id=str(self.session_id), sender_user_id=self.user_id
        )
        self.version = "1"
        self.revoked = False
        self.delay = 0.0
        self.hang = False
        self.entered = asyncio.Event()
        self.authorization_cancelled = False
        self.store = MemoryPresence(str(self.agent_id), {})
        self.authorize = AsyncMock(side_effect=self._authorize)
        router = realtime.RealtimeRouter()
        router.instance_id = "local"
        monkeypatch.setattr(websocket, "realtime_router", router)
        monkeypatch.setattr(websocket.manager, "active_connections", {str(self.agent_id): []})
        monkeypatch.setattr(realtime, "get_redis", self._redis)
        monkeypatch.setattr(access_cache, "read_acl_version", AsyncMock(side_effect=lambda _agent: self.version))
        monkeypatch.setattr(chat_tickets, "authorize", self.authorize)
        monkeypatch.setattr(chat_tickets, "authorized_session", AsyncMock())

    async def _redis(self) -> MemoryPresence:
        return self.store

    async def _authorize(self, _binding: chat_tickets.ChatTicketBinding) -> tuple[object, object, str]:
        self.entered.set()
        revoked = self.revoked  # read at the start, like the database snapshot a slow authorization works from
        try:
            await (asyncio.Event().wait() if self.hang else asyncio.sleep(self.delay))
        except asyncio.CancelledError:
            self.authorization_cancelled = True
            raise
        if revoked:
            raise HTTPException(403, "Permission revoked")
        return object(), object(), "use"

    async def connect(self, *, stalled: bool = False) -> Link:
        link = Link(stalled=stalled)
        await link.socket.accept()
        link.frames.clear()
        if self.shared:
            link.socket.state.chat_binding = chat_tickets.ChatTicketBinding(
                user_id=self.user_id,
                tenant_id=uuid4(),
                agent_id=self.agent_id,
                session_id=self.session_id,
                auth_deadline=int(time.time()) + 3600,
                auth_generation=0,
            )
        websocket.manager.active_connections[str(self.agent_id)].append(
            (link.socket, str(self.session_id), str(self.user_id))
        )
        return link

    async def publish(self, *seqs: int) -> None:
        for seq in seqs:
            delta = streaming.GatewayDelta.model_validate(
                {"kind": "delta", "messageId": str(self.message.id), "seq": seq, "text": f"t{seq}", "replace": False}
            )
            await delivery.publish_chunk(self.message, delta)


async def test_slow_shared_authorization_cannot_starve_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    pipe = Pipeline(monkeypatch, shared=True)
    link = await pipe.connect()
    pipe.delay = SLOW

    await asyncio.wait_for(pipe.publish(0, 1, 2), 10)

    assert link.seqs() == [0, 1, 2]
    pipe.authorize.assert_awaited_once()


async def test_slow_presence_read_still_routes_the_chunk_to_the_remote_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipe = Pipeline(monkeypatch, shared=False)
    link = await pipe.connect()
    remote_record = {"instance_id": "remote", "session_id": str(pipe.session_id), "user_id": str(pipe.user_id)}
    pipe.store = SlowPresence(str(pipe.agent_id), {"remote-connection": remote_record})
    remote = websocket.ConnectionManager()
    remote_link = Link()
    await remote_link.socket.accept()
    remote_link.frames.clear()
    remote.active_connections[str(pipe.agent_id)] = [(remote_link.socket, str(pipe.session_id), str(pipe.user_id))]

    await asyncio.wait_for(pipe.publish(0), 10)
    for _channel, envelope in pipe.store.published:
        await realtime.RealtimeRouter._deliver_message(envelope, remote.deliver_pubsub_message)

    assert [channel for channel, _ in pipe.store.published] == ["realtime:ws:instance:remote"]
    assert link.seqs() == [0]
    assert remote_link.frames == link.frames


async def test_stalled_socket_is_bounded_and_does_not_block_other_recipients(monkeypatch: pytest.MonkeyPatch) -> None:
    pipe = Pipeline(monkeypatch, shared=False)
    stalled = await pipe.connect(stalled=True)
    healthy = await pipe.connect()
    remote_record = {"instance_id": "remote", "session_id": str(pipe.session_id), "user_id": str(pipe.user_id)}
    pipe.store = MemoryPresence(str(pipe.agent_id), {"remote-connection": remote_record})
    started = time.monotonic()

    await asyncio.wait_for(pipe.publish(0), 10)

    assert time.monotonic() - started < 3
    assert healthy.seqs() == [0]
    assert stalled.send_cancelled
    assert len(pipe.store.published) == 1


async def test_revocation_during_slow_authorization_refuses_every_later_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    pipe = Pipeline(monkeypatch, shared=True)
    link = await pipe.connect()
    pipe.delay = SLOW

    async def revoke_midway() -> None:
        await pipe.entered.wait()
        pipe.version, pipe.revoked = "2", True

    await asyncio.wait_for(asyncio.gather(pipe.publish(0), revoke_midway()), 10)
    pipe.delay = 0.0
    await asyncio.wait_for(pipe.publish(1, 2), 10)

    assert link.seqs() == [0]
    assert pipe.authorize.await_count == 3


async def test_cancelling_publication_cancels_the_in_flight_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    pipe = Pipeline(monkeypatch, shared=True)
    link = await pipe.connect()
    pipe.hang = True
    task = asyncio.create_task(pipe.publish(0))
    await asyncio.wait_for(pipe.entered.wait(), 5)

    _ = task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)

    assert pipe.authorization_cancelled
    assert not link.frames
    assert asyncio.all_tasks() == {asyncio.current_task()}


async def test_terminal_drain_is_bounded_when_chunk_delivery_never_answers(monkeypatch, tmp_path) -> None:
    turn = _Turn(monkeypatch, tmp_path)
    turn.message.conversation_id = str(uuid4())
    abandoned = []

    async def web(_message):
        return True

    async def never_answers(*_args, **_kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            abandoned.append(True)
            raise

    def execute(relay, _container, _payload, _cancel):
        for seq in range(6):
            relay.offer(json.dumps({"kind": "delta", "messageId": turn.message_id, "seq": seq, "text": "x"}))
        return wire_terminal(turn.message_id)

    monkeypatch.setattr(rpc, "streams_to_web", web)
    monkeypatch.setattr(streaming.DeltaRelay, "execute", execute)
    monkeypatch.setattr(websocket.manager, "send_to_session", never_answers)
    monkeypatch.setattr(streaming, "DRAIN_SECONDS", 0.2)

    outcome = await asyncio.wait_for(turn.run(), 3)

    assert isinstance(outcome, rpc.TerminalOutcome)
    assert abandoned == [True]
    assert asyncio.all_tasks() == {asyncio.current_task()}


async def test_slow_routing_delivers_the_first_and_the_latest_chunk_without_a_backlog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipe = Pipeline(monkeypatch, shared=False)
    link = await pipe.connect()
    store = GatedPresence(str(pipe.agent_id), {})
    pipe.store = store
    relay = streaming.DeltaRelay(pipe.message, None)
    probe = IngestProbe(monkeypatch, relay)
    consumer = asyncio.create_task(relay.publish())
    try:
        relay.offer(delta_line(pipe.message, 0, "t0"))
        await asyncio.wait_for(store.entered.wait(), 5)
        for seq in range(1, 41):
            relay.offer(delta_line(pipe.message, seq, f"t{seq}"))
        await probe.ingested(40)

        store.release.set()
        await asyncio.wait_for(relay.drain(consumer), 5)
        await consumer
    finally:
        consumer.cancel()
        with suppress(asyncio.CancelledError):
            await consumer

    assert [json.loads(str(frame["text"])) for frame in link.frames if frame["type"] == "websocket.send"] == [
        {"type": "chunk", "content": "t0", "turn_id": str(pipe.message.id), "seq": 0, "replace": False},
        {
            "type": "chunk",
            "content": "".join(f"t{seq}" for seq in range(1, 41)),
            "turn_id": str(pipe.message.id),
            "seq": 40,
            "replace": False,
        },
    ]
    assert relay.dropped == 0
