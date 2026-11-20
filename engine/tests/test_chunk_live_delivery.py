"""Chunk live delivery through the real publish_chunk, ConnectionManager, RealtimeRouter and delivery authorization."""

import asyncio
import json
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager, suppress
from types import SimpleNamespace
from typing import cast, override
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException, WebSocket
from psycopg_pool import PoolTimeout
from redis.exceptions import RedisError
from starlette.types import Message

from app.api import websocket
from app.core import access_cache
from app.db import session as db_session
from app.db.errors import DbOverloadError
from app.records.gateway_message import GatewayMessageRecord
from app.services import (
    chat_tickets,
    openclaw_gateway_delivery as delivery,
    openclaw_gateway_rpc as rpc,
    openclaw_gateway_stream as streaming,
)
from app.services.realtime_runtime import chat_authorization, fanout, router as realtime
from app.services.realtime_runtime.chat_authorization import DeliveryVerdict
from app.services.realtime_runtime.contracts import RoutingOutcome
from app.services.shared_chat import shared_session_id
from test_openclaw_gateway_coalescing import IngestProbe, delta_line
from test_openclaw_gateway_rpc import _Turn, wire_terminal
from test_websocket_session_query import MemoryPresence

# Healthy but slower than the one-second publication deadline this suite guards against regressing to.
SLOW = 1.3
# Failures of what a shared-session decision reads through: each skips one event and never closes the socket.
TRANSIENT_FAILURES: dict[str, Callable[[], Exception]] = {
    "transfer-gate-423": lambda: HTTPException(423, {"code": "transfer_cleanup_pending", "retryable": True}),
    "unavailable-503": lambda: HTTPException(503, "Chat authentication unavailable"),
    "db-overload": DbOverloadError,
    "pool-timeout": lambda: PoolTimeout("couldn't get a connection after 5.00 sec"),
    "redis": lambda: RedisError("Connection reset by peer"),
    "os": lambda: OSError("Network is unreachable"),
}


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
    """A fake socket transport that records frames and can stall, gate or fail text sends.

    ``stalled`` stalls every text send and ``stalls`` only the listed attempts (0-based), like a phone that misses a
    few frames and then reads again. Close codes are recorded as they are sent.
    """

    def __init__(
        self,
        *,
        stalled: bool = False,
        gate: Gate | None = None,
        fail: bool = False,
        stalls: frozenset[int] = frozenset(),
    ) -> None:
        self.frames: list[Message] = []
        self.stalled = stalled
        self.stalls = stalls
        self.gate = gate
        self.fail = fail
        self.attempts = 0
        self.closes: list[int] = []
        self.send_cancelled = False
        self.sent = asyncio.Event()
        self.socket = WebSocket({"type": "websocket", "headers": []}, self._receive, self._send)

    async def _receive(self) -> Message:
        return {"type": "websocket.connect"}

    async def _send(self, message: Message) -> None:
        if message["type"] == "websocket.send":
            attempt = self.attempts
            self.attempts += 1
            if self.fail:
                raise RuntimeError("socket closed")
            try:
                if self.stalled or attempt in self.stalls:
                    await asyncio.Event().wait()
                if self.gate is not None:
                    await self.gate.pass_through()
            except asyncio.CancelledError:
                self.send_cancelled = True
                raise
            self.sent.set()
        elif message["type"] == "websocket.close":
            self.closes.append(message["code"])
        self.frames.append(message)

    def sends(self) -> list[Message]:
        return [frame for frame in self.frames if frame["type"] == "websocket.send"]

    def seqs(self) -> list[int]:
        return [json.loads(str(frame["text"]))["seq"] for frame in self.sends()]


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


class HeldDecisionTime:
    """The shared-session decision clock, where every wait runs out in full while the decision is held at its gate."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, gate: Gate) -> None:
        self.now = 0.0
        self.gate = gate
        self.waits: list[float] = []
        monkeypatch.setattr(chat_authorization, "_clock", lambda: self.now)
        monkeypatch.setattr(chat_authorization, "_wait", self._wait)

    async def _wait(self, _decision: asyncio.Task[DeliveryVerdict], seconds: float) -> None:
        self.waits.append(seconds)
        await self.gate.saturated.wait()
        self.now += seconds


def decisions() -> list[asyncio.Task[object]]:
    return [task for task in asyncio.all_tasks() if task.get_name() == "shared-chat-authorization"]


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
        self.failure: Exception | None = None
        self.authorization_cancelled = False
        self.checkouts = 0
        self.inherited: list[object] = []
        self.store = MemoryPresence(str(self.agent_id), {})
        self.authorize = AsyncMock(side_effect=self._authorize)
        self.outcomes: list[RoutingOutcome] = []
        self.router = realtime.RealtimeRouter()
        self.router.instance_id = "local"
        self.manager = websocket.ConnectionManager()
        self.manager.active_connections[str(self.agent_id)] = []
        monkeypatch.setattr(websocket, "realtime_router", self.router)
        monkeypatch.setattr(websocket, "manager", self.manager)
        monkeypatch.setattr(realtime, "get_redis", self._redis)
        self.read_version = AsyncMock(side_effect=lambda _agent: self.version)
        monkeypatch.setattr(access_cache, "read_acl_version", self.read_version)
        monkeypatch.setattr(chat_tickets, "authorize_identity", self.authorize)
        monkeypatch.setattr(chat_tickets, "authorized_session", AsyncMock())
        monkeypatch.setattr(chat_authorization, "connection_ctx", self._checkout)

    async def _redis(self) -> MemoryPresence:
        return self.store

    @asynccontextmanager
    async def _checkout(self) -> AsyncGenerator[None]:
        """One pool checkout per decision; records any connection the decision would have joined instead."""
        self.checkouts += 1
        self.inherited.append(db_session._conn_ctx.get())
        yield

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
        if self.failure is not None:
            raise self.failure
        if revoked:
            raise HTTPException(403, "Permission revoked")
        return object(), object(), "use"

    async def connect(
        self,
        *,
        stalled: bool = False,
        gate: Gate | None = None,
        fail: bool = False,
        stalls: frozenset[int] = frozenset(),
        tenant_id: UUID | None = None,
        auth_deadline: int | None = None,
    ) -> Link:
        """``tenant_id`` gives a shared socket the identity of an earlier one; by default each is its own viewer."""
        link = Link(stalled=stalled, gate=gate, fail=fail, stalls=stalls)
        await link.socket.accept()
        link.frames.clear()
        if self.shared:
            link.socket.state.chat_binding = chat_tickets.ChatTicketBinding(
                user_id=self.user_id,
                tenant_id=tenant_id or uuid4(),
                agent_id=self.agent_id,
                session_id=self.session_id,
                auth_deadline=int(time.time()) + 3600 if auth_deadline is None else auth_deadline,
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
            self.outcomes.append(await delivery.publish_chunk(self.message, delta))


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
    await asyncio.wait_for(pipe.manager.drain_evictions(), 5)

    assert [link.seqs() for link in links] == [[0], [0]]
    # The next event re-decided each socket once and closed both with 4003; a closed socket is never decided again.
    assert [link.closes for link in links] == [[4003], [4003]]
    assert pipe.authorize.await_count == 2 + 2
    assert pipe.manager.active_connections[str(pipe.agent_id)] == []
    # A revoked viewer is not a lost recipient.
    assert [(outcome.local_targets, outcome.delivered, outcome.dropped) for outcome in pipe.outcomes] == [
        (2, 2, False),
        (2, 0, False),
        (0, 0, False),
    ]


async def test_one_viewers_sockets_share_one_decision_on_one_fresh_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given three cold sockets of one viewer and one of another, published to from inside a held connection.
    pipe = Pipeline(monkeypatch, shared=True)
    pipe.auth_gate = Gate(saturate_at=2)
    viewer = uuid4()
    tabs = [await pipe.connect(tenant_id=viewer) for _ in range(3)]
    other = await pipe.connect()
    held = db_session._conn_ctx.set(cast("db_session.DbConnection", object()))
    try:
        # When a chunk goes out while the decisions are running, then another after they allowed.
        first = asyncio.create_task(pipe.publish(0))
        await asyncio.wait_for(pipe.auth_gate.saturated.wait(), 5)
        pipe.auth_gate.open.set()
        await asyncio.wait_for(first, 5)
        await asyncio.wait_for(pipe.publish(1), 5)
    finally:
        db_session._conn_ctx.reset(held)

    # Then each identity was decided once, on a checkout of its own rather than the publisher's connection, and every
    # socket got both chunks in order, the second from its own cache.
    assert pipe.authorize.await_count == pipe.checkouts == pipe.auth_gate.peak == 2
    assert pipe.inherited == [None, None]
    assert [link.seqs() for link in [*tabs, other]] == [[0, 1]] * 4


async def test_a_decision_that_outlasts_the_chunk_wait_still_fills_the_cache_and_later_chunks_skip_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a cold socket whose decision is held past every wait.
    pipe = Pipeline(monkeypatch, shared=True)
    pipe.auth_gate = Gate()
    clock = HeldDecisionTime(monkeypatch, pipe.auth_gate)
    link = await pipe.connect()

    # When two chunks are published while it is held.
    await asyncio.wait_for(pipe.publish(0, 1), 5)

    # Then the first waited out the whole chunk wait and the second, counted from the same start, did not wait at all;
    # both skipped the socket and count as dropped, and the decision was neither cancelled nor started again.
    assert clock.waits == [realtime.CHUNK_AUTHORIZATION_WAIT_SECONDS] == [2.0]
    assert [(outcome.skipped, outcome.dropped) for outcome in pipe.outcomes] == [(1, True), (1, True)]
    assert not link.frames
    pipe.authorize.assert_awaited_once()
    assert not pipe.authorization_cancelled

    # When the decision allows after every waiter gave up.
    (decision,) = decisions()
    pipe.auth_gate.open.set()
    assert await asyncio.wait_for(decision, 5) is DeliveryVerdict.ALLOWED

    # Then it filled the socket's cache: the next chunk goes out with no new decision and no wait.
    await asyncio.wait_for(pipe.publish(2), 5)
    assert link.seqs() == [2]
    pipe.authorize.assert_awaited_once()
    assert clock.waits == [2.0]


async def test_every_wait_counts_from_the_decision_start_whatever_the_event(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a cold socket whose decision is held past every wait.
    pipe = Pipeline(monkeypatch, shared=True)
    pipe.auth_gate = Gate()
    clock = HeldDecisionTime(monkeypatch, pipe.auth_gate)
    link = await pipe.connect()
    agent_id, session_id = str(pipe.agent_id), str(pipe.session_id)
    events: list[dict[str, object]] = [
        {"type": "status", "content": "thinking"},
        {"type": "done", "role": "assistant", "content": "x"},
        {"type": "error", "content": "failed"},
    ]

    # When a chunk, a status, a done and an error arrive while it is held.
    await asyncio.wait_for(pipe.publish(0), 5)
    for event in events:
        pipe.outcomes.append(await asyncio.wait_for(pipe.manager.send_to_session(agent_id, session_id, event), 5))

    # Then each waited only what was left of its own bound (2 s, 5 s, 15 s) since the decision started.
    assert clock.waits == [2.0, 3.0, 10.0]
    assert [outcome.skipped for outcome in pipe.outcomes] == [1, 1, 1, 1]
    assert not link.frames
    pipe.authorize.assert_awaited_once()
    pipe.auth_gate.open.set()
    (decision,) = decisions()
    assert await asyncio.wait_for(decision, 5) is DeliveryVerdict.ALLOWED


async def test_an_expired_socket_closes_with_4001_while_the_same_viewers_live_one_still_receives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given two sockets of one viewer, one past its own deadline.
    pipe = Pipeline(monkeypatch, shared=True)
    viewer = uuid4()
    live = await pipe.connect(tenant_id=viewer)
    expired = await pipe.connect(tenant_id=viewer, auth_deadline=int(time.time()) - 1)

    # When a chunk goes out, and another after the live socket's deadline passes too.
    await asyncio.wait_for(pipe.publish(0), 5)
    live.socket.state.chat_binding.auth_deadline = int(time.time()) - 1
    await asyncio.wait_for(pipe.publish(1), 5)
    await asyncio.wait_for(pipe.manager.drain_evictions(), 5)

    # Then the deadline was checked per socket and before the cached decision: one decision, two 4001 closes.
    assert (live.seqs(), expired.seqs()) == ([0], [])
    assert (live.closes, expired.closes) == ([4001], [4001])
    pipe.authorize.assert_awaited_once()


@pytest.mark.parametrize("failure", TRANSIENT_FAILURES)
async def test_a_transient_failure_skips_only_that_chunk_and_keeps_the_socket(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    pipe = Pipeline(monkeypatch, shared=True)
    link = await pipe.connect()
    pipe.failure = TRANSIENT_FAILURES[failure]()

    await asyncio.wait_for(pipe.publish(0), 5)
    pipe.failure = None
    await asyncio.wait_for(pipe.publish(1), 5)
    await asyncio.wait_for(pipe.manager.drain_evictions(), 5)

    assert link.seqs() == [1]
    assert not link.closes
    assert pipe.manager.is_live(link.socket)
    assert [(outcome.skipped, outcome.dropped) for outcome in pipe.outcomes] == [(1, True), (0, False)]
    # The failure was never cached: the next chunk decided again.
    assert pipe.authorize.await_count == 2


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

    async def connect(
        self, *, stalled: bool = False, gate: Gate | None = None, stalls: frozenset[int] = frozenset()
    ) -> Link:
        link = Link(stalled=stalled, gate=gate, stalls=stalls)
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
async def test_the_chunk_bound_applies_to_chunk_events_only(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
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


async def test_stalled_done_socket_is_bounded_and_the_next_event_still_reaches_healthy_sockets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(realtime, "TERMINAL_SEND_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(websocket, "CHUNK_SEND_TIMEOUT_SECONDS", 0.01)
    pipe = Pipeline(monkeypatch, shared=False)
    receiver = Receiver(pipe)
    stalled = await receiver.connect(stalled=True)
    healthy = await receiver.connect()
    turn_id = str(uuid4())
    events: list[dict[str, object]] = [
        {"type": "done", "role": "assistant", "content": "x", "turn_id": turn_id},
        {"type": "chunk", "content": "y", "turn_id": str(uuid4()), "seq": 0, "replace": False},
        {"type": "error", "content": "z"},
    ]

    for event in events:
        await asyncio.wait_for(receiver.manager.deliver_pubsub_message(agent_id=str(pipe.agent_id), payload=event), 5)
    await asyncio.wait_for(receiver.manager.drain_evictions(), 5)

    assert [json.loads(str(frame["text"]))["type"] for frame in healthy.frames] == ["done", "chunk", "error"]
    assert stalled.send_cancelled
    assert not stalled.sends()
    assert asyncio.all_tasks() == {asyncio.current_task()}


async def test_stalled_done_socket_cannot_hold_back_the_publisher_or_remote_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(realtime, "TERMINAL_SEND_TIMEOUT_SECONDS", 0.01)
    pipe = Pipeline(monkeypatch, shared=False)
    pipe.store = remote_presence(pipe)
    stalled = await pipe.connect(stalled=True)
    healthy = await pipe.connect()

    await asyncio.wait_for(
        websocket.manager.send_to_session(
            str(pipe.agent_id), str(pipe.session_id), {"type": "done", "role": "assistant", "content": "x"}
        ),
        5,
    )

    assert [json.loads(str(frame["text"]))["type"] for frame in healthy.frames] == ["done"]
    assert stalled.send_cancelled
    assert len(pipe.store.published) == 1


async def test_terminal_send_bound_never_cancels_shared_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(realtime, "TERMINAL_SEND_TIMEOUT_SECONDS", 0.0)  # expires at the first suspension of a send
    pipe = Pipeline(monkeypatch, shared=True)
    pipe.auth_gate = Gate()
    link = await pipe.connect()

    task = asyncio.create_task(
        websocket.manager.send_to_session(
            str(pipe.agent_id), str(pipe.session_id), {"type": "done", "role": "assistant", "content": "x"}
        )
    )
    await asyncio.wait_for(pipe.auth_gate.saturated.wait(), 5)
    pipe.auth_gate.open.set()
    await asyncio.wait_for(task, 5)

    assert not pipe.authorization_cancelled
    assert [json.loads(str(frame["text"]))["type"] for frame in link.frames] == ["done"]


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
    await asyncio.wait_for(pipe.manager.drain_evictions(), 5)
    await asyncio.wait_for(receiver.manager.drain_evictions(), 5)

    assert healthy.seqs() == remote_healthy.seqs() == [0, 1, 2, 3]
    assert stalled.send_cancelled
    assert remote_stalled.send_cancelled
    assert not stalled.sends()
    assert not remote_stalled.sends()


async def test_publisher_evicts_a_stalled_socket_after_consecutive_timeouts_and_keeps_the_healthy_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(delivery, "CHUNK_SEND_TIMEOUT_SECONDS", 0.0)  # expires at the first suspension of a send
    threshold = websocket.STALLED_SEND_EVICTION_THRESHOLD
    pipe = Pipeline(monkeypatch, shared=False)
    stalled = await pipe.connect(stalled=True)
    healthy = await pipe.connect()
    seqs = list(range(threshold + 2))

    await asyncio.wait_for(pipe.publish(*seqs), 10)
    await asyncio.wait_for(pipe.manager.drain_evictions(), 5)

    assert healthy.seqs() == seqs
    # Only the sends before eviction waited on the stalled socket; later chunks never tried it.
    assert stalled.attempts == threshold
    assert not stalled.sends()
    assert stalled.closes == [websocket.EVICTION_CLOSE_CODE]
    assert [outcome.evicted for outcome in pipe.outcomes] == [0] * (threshold - 1) + [1, 0, 0]
    assert [outcome.dropped for outcome in pipe.outcomes] == [True] * threshold + [False, False]
    assert [ws for ws, _, _ in pipe.manager.active_connections[str(pipe.agent_id)]] == [healthy.socket]
    assert asyncio.all_tasks() == {asyncio.current_task()}


async def test_receiver_evicts_a_stalled_socket_and_the_healthy_one_keeps_every_chunk_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(websocket, "CHUNK_SEND_TIMEOUT_SECONDS", 0.0)  # expires at the first suspension of a send
    threshold = websocket.STALLED_SEND_EVICTION_THRESHOLD
    pipe = Pipeline(monkeypatch, shared=False)
    pipe.store = remote_presence(pipe)
    seqs = list(range(threshold + 2))
    await asyncio.wait_for(pipe.publish(*seqs), 5)
    receiver = Receiver(pipe)
    stalled = await receiver.connect(stalled=True)
    healthy = await receiver.connect()

    await asyncio.wait_for(receiver.receive(), 10)
    await asyncio.wait_for(receiver.manager.drain_evictions(), 5)

    assert healthy.seqs() == seqs
    assert stalled.attempts == threshold
    assert not stalled.sends()
    assert stalled.closes == [websocket.EVICTION_CLOSE_CODE]
    assert [ws for ws, _, _ in receiver.manager.active_connections[str(pipe.agent_id)]] == [healthy.socket]
    assert asyncio.all_tasks() == {asyncio.current_task()}


@pytest.mark.parametrize("path", ["publisher", "receiver"])
async def test_a_slow_but_healthy_socket_that_reads_again_between_timeouts_is_never_evicted(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    monkeypatch.setattr(delivery, "CHUNK_SEND_TIMEOUT_SECONDS", 0.0)
    monkeypatch.setattr(websocket, "CHUNK_SEND_TIMEOUT_SECONDS", 0.0)
    threshold = websocket.STALLED_SEND_EVICTION_THRESHOLD
    pipe = Pipeline(monkeypatch, shared=False)
    seqs = list(range(2 * threshold))
    # One timeout short of eviction, then one send that goes through, twice over.
    stalls = frozenset(seq for seq in seqs if seq % threshold != threshold - 1)
    if path == "publisher":
        manager = pipe.manager
        slow = await pipe.connect(stalls=stalls)
        healthy = await pipe.connect()
        await asyncio.wait_for(pipe.publish(*seqs), 10)
        assert not any(outcome.evicted for outcome in pipe.outcomes)
    else:
        pipe.store = remote_presence(pipe)
        await asyncio.wait_for(pipe.publish(*seqs), 10)
        receiver = Receiver(pipe)
        manager = receiver.manager
        slow = await receiver.connect(stalls=stalls)
        healthy = await receiver.connect()
        await asyncio.wait_for(receiver.receive(), 10)

    assert healthy.seqs() == seqs
    assert slow.seqs() == [threshold - 1, 2 * threshold - 1]
    assert slow.attempts == len(seqs)
    assert slow.closes == []
    assert manager.is_live(slow.socket)
    assert slow.socket in [ws for ws, _, _ in manager.active_connections[str(pipe.agent_id)]]
    assert asyncio.all_tasks() == {asyncio.current_task()}


async def test_a_snapshot_taken_before_eviction_sends_nothing_more_to_the_evicted_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(delivery, "CHUNK_SEND_TIMEOUT_SECONDS", 0.0)
    threshold = websocket.STALLED_SEND_EVICTION_THRESHOLD
    pipe = Pipeline(monkeypatch, shared=False)
    stalled = await pipe.connect(stalled=True)
    healthy = await pipe.connect()
    snapshot = list(pipe.manager.active_connections[str(pipe.agent_id)])
    await asyncio.wait_for(pipe.publish(*range(threshold)), 10)

    # A delivery that copied the connection list before the eviction, with a bound it must never pay.
    late: dict[str, object] = {
        "type": "chunk",
        "content": "late",
        "turn_id": str(pipe.message.id),
        "seq": threshold,
        "replace": False,
    }
    outcome = await asyncio.wait_for(
        pipe.router.route_message(
            agent_id=str(pipe.agent_id),
            message=late,
            local_connections=snapshot,
            session_id=str(pipe.session_id),
            send_timeout=60.0,
            observer=pipe.manager,
        ),
        5,
    )
    await asyncio.wait_for(pipe.manager.drain_evictions(), 5)

    assert stalled.attempts == threshold
    assert healthy.seqs() == [*range(threshold), threshold]
    assert (outcome.local_targets, outcome.delivered, outcome.failed, outcome.evicted) == (2, 1, 0, 1)
    assert outcome.dropped


async def test_receiver_skips_a_socket_evicted_while_its_delivery_was_authorizing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(websocket, "CHUNK_SEND_TIMEOUT_SECONDS", 60.0)  # a send the stalled socket must never get
    monkeypatch.setattr(websocket, "EVICTION_CLOSE_TIMEOUT_SECONDS", 60.0)
    pipe = Pipeline(monkeypatch, shared=False)
    receiver = Receiver(pipe)
    stalled = await receiver.connect(stalled=True)
    stalled.socket.state.realtime_connection_id = "stalled-connection"
    healthy = await receiver.connect()
    gate = Gate(saturate_at=2)
    removal = asyncio.Event()

    async def authorize(
        _ws: WebSocket, _agent: str, _session: str | None, acl_version: str | None = None, *, wait_seconds: float
    ) -> DeliveryVerdict:
        await gate.pass_through()
        return DeliveryVerdict.ALLOWED

    async def remove_presence(*, agent_id: str, connection_id: str) -> None:
        assert (agent_id, connection_id) == (str(pipe.agent_id), "stalled-connection")
        await removal.wait()  # the Redis round trip that comes before the eviction's close

    monkeypatch.setattr(websocket, "authorize_chat_delivery", authorize)
    monkeypatch.setattr(pipe.router, "unregister_connection_id", remove_presence)
    payload: dict[str, object] = {"type": "chunk", "content": "x", "turn_id": str(uuid4()), "seq": 0, "replace": False}
    task = asyncio.create_task(receiver.manager.deliver_pubsub_message(agent_id=str(pipe.agent_id), payload=payload))
    await asyncio.wait_for(gate.saturated.wait(), 5)

    for _ in range(websocket.STALLED_SEND_EVICTION_THRESHOLD):
        _ = receiver.manager.timed_out(str(pipe.agent_id), stalled.socket)
    gate.open.set()
    await asyncio.wait_for(task, 5)
    removal.set()
    await asyncio.wait_for(receiver.manager.drain_evictions(), 5)

    assert stalled.attempts == 0
    assert healthy.seqs() == [0]
    assert stalled.closes == [websocket.EVICTION_CLOSE_CODE]


async def test_revocation_during_slow_authorization_refuses_every_later_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    pipe = Pipeline(monkeypatch, shared=True)
    pipe.auth_gate = Gate()
    link = await pipe.connect()
    first = asyncio.create_task(pipe.publish(0))
    await asyncio.wait_for(pipe.auth_gate.saturated.wait(), 5)

    # The revocation lands while the decision works from the snapshot it read before it.
    pipe.version, pipe.revoked = "2", True
    pipe.auth_gate.open.set()
    await asyncio.wait_for(first, 5)
    await asyncio.wait_for(pipe.publish(1, 2), 5)
    await asyncio.wait_for(pipe.manager.drain_evictions(), 5)

    # The bumped version re-decided the next chunk, which closed the socket, so the last chunk had nobody to decide.
    assert link.seqs() == [0]
    assert link.closes == [4003]
    assert pipe.authorize.await_count == 2


async def test_cancelling_publication_leaves_the_decision_running_until_the_router_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipe = Pipeline(monkeypatch, shared=True)
    link = await pipe.connect()
    pipe.hang = True
    task = asyncio.create_task(pipe.publish(0))
    await asyncio.wait_for(pipe.entered.wait(), 5)

    _ = task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)

    # The waiter gave up; the decision it joined did not, so the next event joins it instead of starting another.
    assert not pipe.authorization_cancelled
    assert len(decisions()) == 1

    # Stopping the router, which shutdown does before the pool closes, cancels and awaits it.
    await asyncio.wait_for(pipe.router.stop(), 5)

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
