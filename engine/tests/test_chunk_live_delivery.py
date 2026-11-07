"""Chunk live delivery through the real publish_chunk, ConnectionManager, RealtimeRouter and delivery authorization."""

import asyncio
import json
import time
from contextlib import suppress
from types import SimpleNamespace
from typing import override
from unittest.mock import AsyncMock, Mock
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
from app.services.realtime_runtime import fanout, router as realtime
from app.services.shared_chat import shared_session_id
from test_openclaw_gateway_coalescing import IngestProbe, delta_line
from test_openclaw_gateway_rpc import _Turn, wire_terminal
from test_websocket_session_query import MemoryPresence

# Healthy but slower than the one-second publication deadline this suite guards against regressing to.
SLOW = 1.3


class Gate:
    """Holds every passer until opened; records how many are held at once."""

    def __init__(self, saturate_at: int = 1) -> None:
        self.open = asyncio.Event()
        self.saturated = asyncio.Event()
        self.saturate_at = saturate_at
        self.in_flight = 0
        self.peak = 0

    async def pass_through(self) -> None:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        if self.in_flight >= self.saturate_at:
            self.saturated.set()
        try:
            await self.open.wait()
        finally:
            self.in_flight -= 1


class Link:
    """A fake socket transport that records frames and can stall, gate or fail every text send."""

    def __init__(self, *, stalled: bool = False, gate: Gate | None = None, fail: bool = False) -> None:
        self.frames: list[Message] = []
        self.stalled = stalled
        self.gate = gate
        self.fail = fail
        self.send_cancelled = False
        self.sent = asyncio.Event()
        self.socket = WebSocket({"type": "websocket", "headers": []}, self._receive, self._send)

    async def _receive(self) -> Message:
        return {"type": "websocket.connect"}

    async def _send(self, message: Message) -> None:
        if message["type"] == "websocket.send":
            if self.fail:
                raise RuntimeError("socket closed")
            try:
                if self.stalled:
                    await asyncio.Event().wait()
                if self.gate is not None:
                    await self.gate.pass_through()
            except asyncio.CancelledError:
                self.send_cancelled = True
                raise
            self.sent.set()
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
        self.auth_gate: Gate | None = None
        self.entered = asyncio.Event()
        self.authorization_cancelled = False
        self.store = MemoryPresence(str(self.agent_id), {})
        self.authorize = AsyncMock(side_effect=self._authorize)
        router = realtime.RealtimeRouter()
        router.instance_id = "local"
        monkeypatch.setattr(websocket, "realtime_router", router)
        monkeypatch.setattr(websocket.manager, "active_connections", {str(self.agent_id): []})
        monkeypatch.setattr(realtime, "get_redis", self._redis)
        self.read_version = AsyncMock(side_effect=lambda _agent: self.version)
        monkeypatch.setattr(access_cache, "read_acl_version", self.read_version)
        monkeypatch.setattr(chat_tickets, "authorize", self.authorize)
        monkeypatch.setattr(chat_tickets, "authorized_session", AsyncMock())

    async def _redis(self) -> MemoryPresence:
        return self.store

    async def _authorize(self, _binding: chat_tickets.ChatTicketBinding) -> tuple[object, object, str]:
        self.entered.set()
        revoked = self.revoked  # read at the start, like the database snapshot a slow authorization works from
        try:
            if self.auth_gate is not None:
                await self.auth_gate.pass_through()
            await (asyncio.Event().wait() if self.hang else asyncio.sleep(self.delay))
        except asyncio.CancelledError:
            self.authorization_cancelled = True
            raise
        if revoked:
            raise HTTPException(403, "Permission revoked")
        return object(), object(), "use"

    async def connect(self, *, stalled: bool = False, gate: Gate | None = None, fail: bool = False) -> Link:
        link = Link(stalled=stalled, gate=gate, fail=fail)
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
    monkeypatch.setattr(delivery, "CHUNK_SEND_TIMEOUT_SECONDS", 0.01)
    pipe = Pipeline(monkeypatch, shared=False)
    stalled = await pipe.connect(stalled=True)
    healthy = await pipe.connect()
    remote_record = {"instance_id": "remote", "session_id": str(pipe.session_id), "user_id": str(pipe.user_id)}
    pipe.store = MemoryPresence(str(pipe.agent_id), {"remote-connection": remote_record})

    await asyncio.wait_for(pipe.publish(0), 10)

    assert healthy.seqs() == [0]
    assert stalled.send_cancelled
    assert len(pipe.store.published) == 1


async def test_stalled_local_socket_delays_healthy_sockets_only_until_the_send_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(delivery, "CHUNK_SEND_TIMEOUT_SECONDS", 60.0)
    pipe = Pipeline(monkeypatch, shared=False)
    gate = Gate()
    slow = await pipe.connect(gate=gate)
    healthy = [await pipe.connect(), await pipe.connect()]
    remote_record = {"instance_id": "remote", "session_id": str(pipe.session_id), "user_id": str(pipe.user_id)}
    pipe.store = MemoryPresence(str(pipe.agent_id), {"remote-connection": remote_record})

    task = asyncio.create_task(pipe.publish(0))
    await asyncio.wait_for(gate.saturated.wait(), 5)
    for link in healthy:
        await asyncio.wait_for(link.sent.wait(), 5)

    assert not task.done()
    assert not slow.frames
    gate.open.set()
    await asyncio.wait_for(task, 5)
    assert [link.seqs() for link in (slow, *healthy)] == [[0], [0], [0]]
    assert len(pipe.store.published) == 1


async def test_presence_lookup_runs_alongside_the_local_sends(monkeypatch: pytest.MonkeyPatch) -> None:
    pipe = Pipeline(monkeypatch, shared=False)
    store = GatedPresence(str(pipe.agent_id), {})
    store.release.set()
    pipe.store = store
    gate = Gate()
    gate.open = store.entered  # the local send cannot finish until the presence read has started
    link = await pipe.connect(gate=gate)

    await asyncio.wait_for(pipe.publish(0), 5)

    assert link.seqs() == [0]


async def test_one_failing_socket_stops_neither_the_others_nor_remote_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipe = Pipeline(monkeypatch, shared=False)
    broken = await pipe.connect(fail=True)
    healthy = [await pipe.connect(), await pipe.connect()]
    remote_record = {"instance_id": "remote", "session_id": str(pipe.session_id), "user_id": str(pipe.user_id)}
    pipe.store = MemoryPresence(str(pipe.agent_id), {"remote-connection": remote_record})
    debug: list[str] = []
    monkeypatch.setattr(realtime, "logger", SimpleNamespace(debug=debug.append, warning=lambda _message: None))

    await asyncio.wait_for(pipe.publish(0), 5)

    assert not broken.frames
    assert [link.seqs() for link in healthy] == [[0], [0]]
    assert len(pipe.store.published) == 1
    assert "local=2" in debug[0]


async def test_local_fanout_never_runs_more_sends_than_the_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    bound = fanout.LOCAL_FANOUT_CONCURRENCY
    pipe = Pipeline(monkeypatch, shared=False)
    gate = Gate(saturate_at=bound)
    links = [await pipe.connect(gate=gate) for _ in range(bound + 4)]

    task = asyncio.create_task(pipe.publish(0))
    await asyncio.wait_for(gate.saturated.wait(), 5)
    assert gate.in_flight == bound
    gate.open.set()
    await asyncio.wait_for(task, 5)

    assert gate.peak == bound
    assert [link.seqs() for link in links] == [[0]] * (bound + 4)


async def test_cold_shared_authorization_is_bounded_and_reads_the_acl_version_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bound = fanout.LOCAL_FANOUT_CONCURRENCY
    pipe = Pipeline(monkeypatch, shared=True)
    pipe.auth_gate = Gate(saturate_at=bound)
    links = [await pipe.connect() for _ in range(bound + 4)]

    task = asyncio.create_task(pipe.publish(0))
    await asyncio.wait_for(pipe.auth_gate.saturated.wait(), 5)
    assert pipe.auth_gate.in_flight == bound
    pipe.auth_gate.open.set()
    await asyncio.wait_for(task, 5)

    assert pipe.auth_gate.peak == bound
    assert [link.seqs() for link in links] == [[0]] * (bound + 4)
    pipe.read_version.assert_awaited_once()


async def test_ordinary_session_delivery_reads_no_acl_version(monkeypatch: pytest.MonkeyPatch) -> None:
    pipe = Pipeline(monkeypatch, shared=False)
    links = [await pipe.connect() for _ in range(3)]

    await asyncio.wait_for(pipe.publish(0, 1), 5)

    assert [link.seqs() for link in links] == [[0, 1]] * 3
    pipe.read_version.assert_not_awaited()
    pipe.authorize.assert_not_awaited()


async def test_revocation_applies_to_the_next_event_for_every_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    pipe = Pipeline(monkeypatch, shared=True)
    links = [await pipe.connect(), await pipe.connect()]
    await asyncio.wait_for(pipe.publish(0), 5)

    pipe.version, pipe.revoked = "2", True
    await asyncio.wait_for(pipe.publish(1, 2), 5)

    assert [link.seqs() for link in links] == [[0], [0]]
    # Negative decisions are never cached: both later events re-authorized both sockets.
    assert pipe.authorize.await_count == 2 + 2 * 2


async def test_each_event_serializes_once_for_all_local_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    pipe = Pipeline(monkeypatch, shared=False)
    links = [await pipe.connect() for _ in range(4)]
    encode = Mock(wraps=fanout.encode_event_text)
    monkeypatch.setattr(realtime, "encode_event_text", encode)

    await asyncio.wait_for(pipe.publish(0), 5)

    encode.assert_called_once()
    assert len({str(link.frames[0]["text"]) for link in links}) == 1


class Receiver:
    """The receiving instance: its own ConnectionManager fed from the envelopes the publisher put on Redis."""

    def __init__(self, pipe: Pipeline) -> None:
        self.pipe = pipe
        self.manager = websocket.ConnectionManager()
        self.manager.active_connections[str(pipe.agent_id)] = []

    async def connect(self, *, stalled: bool = False, gate: Gate | None = None) -> Link:
        link = Link(stalled=stalled, gate=gate)
        await link.socket.accept()
        link.frames.clear()
        self.manager.active_connections[str(self.pipe.agent_id)].append(
            (link.socket, str(self.pipe.session_id), str(self.pipe.user_id))
        )
        return link

    async def receive(self, start: int = 0) -> None:
        for _channel, envelope in self.pipe.store.published[start:]:
            await realtime.RealtimeRouter._deliver_message(envelope, self.manager.deliver_pubsub_message)


def remote_presence(pipe: Pipeline) -> MemoryPresence:
    record = {"instance_id": "remote", "session_id": str(pipe.session_id), "user_id": str(pipe.user_id)}
    return MemoryPresence(str(pipe.agent_id), {"remote-connection": record})


async def test_stalled_remote_socket_delays_healthy_receivers_only_until_the_send_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(websocket, "CHUNK_SEND_TIMEOUT_SECONDS", 60.0)
    pipe = Pipeline(monkeypatch, shared=False)
    pipe.store = remote_presence(pipe)
    await asyncio.wait_for(pipe.publish(0), 5)
    receiver = Receiver(pipe)
    gate = Gate()
    slow = await receiver.connect(gate=gate)
    healthy = [await receiver.connect(), await receiver.connect()]

    task = asyncio.create_task(receiver.receive())
    await asyncio.wait_for(gate.saturated.wait(), 5)
    for link in healthy:
        await asyncio.wait_for(link.sent.wait(), 5)

    assert not task.done()
    gate.open.set()
    await asyncio.wait_for(task, 5)
    assert [link.seqs() for link in (slow, *healthy)] == [[0], [0], [0]]
    assert asyncio.all_tasks() == {asyncio.current_task()}


@pytest.mark.parametrize("kind", ["chunk", "done"])
async def test_only_chunk_events_get_a_receive_side_send_bound(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    monkeypatch.setattr(websocket, "CHUNK_SEND_TIMEOUT_SECONDS", 0.0)  # expires at the first suspension of a send
    pipe = Pipeline(monkeypatch, shared=False)
    receiver = Receiver(pipe)
    gate = Gate()
    slow = await receiver.connect(gate=gate)
    healthy = await receiver.connect()
    payload: dict[str, object] = (
        {"type": "chunk", "content": "x", "turn_id": str(uuid4()), "seq": 0, "replace": False}
        if kind == "chunk"
        else {"type": "done", "role": "assistant", "content": "x"}
    )

    task = asyncio.create_task(receiver.manager.deliver_pubsub_message(agent_id=str(pipe.agent_id), payload=payload))
    if kind == "done":
        await asyncio.wait_for(gate.saturated.wait(), 5)
        gate.open.set()
    await asyncio.wait_for(task, 5)

    assert healthy.sent.is_set()
    assert slow.send_cancelled is (kind == "chunk")
    assert bool(slow.frames) is (kind == "done")


async def test_receiver_serializes_each_event_once(monkeypatch: pytest.MonkeyPatch) -> None:
    pipe = Pipeline(monkeypatch, shared=False)
    pipe.store = remote_presence(pipe)
    await asyncio.wait_for(pipe.publish(0), 5)
    receiver = Receiver(pipe)
    links = [await receiver.connect() for _ in range(4)]
    encode = Mock(wraps=fanout.encode_event_text)
    monkeypatch.setattr(websocket, "encode_event_text", encode)

    await receiver.receive()

    encode.assert_called_once()
    assert [link.seqs() for link in links] == [[0]] * 4


async def test_chunks_reach_local_and_remote_sockets_in_seq_order_despite_stalled_sockets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(delivery, "CHUNK_SEND_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(websocket, "CHUNK_SEND_TIMEOUT_SECONDS", 0.01)
    pipe = Pipeline(monkeypatch, shared=False)
    pipe.store = remote_presence(pipe)
    stalled = await pipe.connect(stalled=True)
    healthy = await pipe.connect()
    receiver = Receiver(pipe)
    remote_stalled = await receiver.connect(stalled=True)
    remote_healthy = await receiver.connect()

    await asyncio.wait_for(pipe.publish(0, 1, 2, 3), 10)
    await asyncio.wait_for(receiver.receive(), 10)

    assert healthy.seqs() == remote_healthy.seqs() == [0, 1, 2, 3]
    assert stalled.send_cancelled
    assert remote_stalled.send_cancelled
    assert not stalled.frames
    assert not remote_stalled.frames


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
