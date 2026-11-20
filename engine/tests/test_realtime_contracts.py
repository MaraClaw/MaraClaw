import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, call

import pytest
from fastapi import WebSocket
from starlette.types import Message

from app.api import websocket
from app.core.json_types import json_loads_object, object_attr
from app.services.realtime_runtime import router
from app.services.realtime_runtime.chat_authorization import DeliveryVerdict
from app.services.realtime_runtime.contracts import LocalDeliverer, RoutingOutcome


@pytest.mark.parametrize("encoding", ["mapping", "text", "bytes", "bytearray"])
@pytest.mark.parametrize("routing", [{}, {"session_id": None, "user_id": None}, {"session_id": "", "user_id": ""}])
async def test_delivery_preserves_optional_routing_and_dynamic_messages(
    encoding: str, routing: dict[str, object]
) -> None:
    message = {"type": "custom", "nested": {"items": [1, None, False]}, "extra": "value"}
    envelope = {"agent_id": "agent-not-a-uuid", "message": message, **routing, "future": [1, 2]}
    text = json.dumps(envelope)
    formats: dict[str, object] = {
        "mapping": envelope,
        "text": text,
        "bytes": text.encode(),
        "bytearray": bytearray(text.encode()),
    }
    delivered: list[dict[str, object]] = []

    async def deliver(
        *, agent_id: str, payload: dict[str, object], session_id: str | None, user_id: str | None
    ) -> None:
        delivered.append({"agent_id": agent_id, "payload": payload, "session_id": session_id, "user_id": user_id})

    callback: LocalDeliverer = deliver
    await router.RealtimeRouter._deliver_message(formats[encoding], callback)

    assert delivered == [
        {
            "agent_id": "agent-not-a-uuid",
            "payload": message,
            "session_id": routing.get("session_id"),
            "user_id": routing.get("user_id"),
        }
    ]


@pytest.mark.parametrize(
    "envelope",
    [
        {"agent_id": 7, "message": {"type": "custom"}},
        {"agent_id": None, "message": [1, 2], "session_id": False},
        {"agent_id": "agent", "message": None, "user_id": 9},
        {"agent_id": "agent", "message": "scalar", "origin_instance_id": ["ignored"]},
    ],
)
async def test_delivery_keeps_legacy_field_value_acceptance(envelope: dict[str, object]) -> None:
    delivered: list[dict[str, object]] = []

    async def deliver(**kwargs: object) -> None:
        delivered.append(kwargs)

    await router.RealtimeRouter._deliver_message(envelope, deliver)

    assert delivered == [
        {
            "agent_id": envelope["agent_id"],
            "payload": envelope["message"],
            "session_id": envelope.get("session_id"),
            "user_id": envelope.get("user_id"),
        }
    ]


@pytest.mark.parametrize(
    ("raw", "error_type"),
    [
        (b"{", json.JSONDecodeError),
        (b"\xff", UnicodeDecodeError),
        ("[]", KeyError),
        ("null", KeyError),
        ([], KeyError),
        ({}, KeyError),
        ({"agent_id": "agent"}, KeyError),
        ({"message": {}}, KeyError),
        ({"agent_id": "agent", "message": {}, "extra": object()}, KeyError),
    ],
)
async def test_malformed_delivery_is_logged_without_invoking_callback(
    monkeypatch: pytest.MonkeyPatch, raw: object, error_type: type[Exception]
) -> None:
    errors: list[Exception] = []

    def warning(_template: str, error: Exception) -> None:
        errors.append(error)

    deliver = AsyncMock()
    monkeypatch.setattr(router.logger, "warning", warning)

    await router.RealtimeRouter._deliver_message(raw, deliver)

    deliver.assert_not_awaited()
    assert len(errors) == 1
    assert isinstance(errors[0], error_type)


async def test_callback_failure_remains_contained(monkeypatch: pytest.MonkeyPatch) -> None:
    warning = Mock()
    deliver = AsyncMock(side_effect=RuntimeError("callback failed"))
    monkeypatch.setattr(router.logger, "warning", warning)

    await router.RealtimeRouter._deliver_message({"agent_id": "agent", "message": {}}, deliver)

    deliver.assert_awaited_once_with(agent_id="agent", payload={}, session_id=None, user_id=None)
    warning.assert_called_once()


async def test_presence_preserves_partial_hashes_empty_strings_and_stale_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hashes: dict[str, dict[str | bytes, str | bytes]] = {
        "realtime:ws:conn:partial": {
            b"instance_id": b"remote",
            b"session_id": b"",
            b"user_id": b"",
            b"future": b"kept",
        },
        "realtime:ws:conn:session": {"session_id": " session "},
        "realtime:ws:conn:stale": {},
    }
    remove = AsyncMock()
    read = Mock()
    pipe = MagicMock()
    pipe.hgetall = read
    pipe.__aenter__ = AsyncMock(return_value=pipe)
    pipe.__aexit__ = AsyncMock(return_value=False)
    pipe.execute = AsyncMock(side_effect=lambda: [hashes[queued.args[0]] for queued in read.call_args_list])
    store = SimpleNamespace(
        smembers=AsyncMock(return_value={b"partial", "session", "stale"}),
        pipeline=Mock(return_value=pipe),
        srem=remove,
    )
    monkeypatch.setattr(router, "get_redis", AsyncMock(return_value=store))
    instance = router.RealtimeRouter()

    records = await instance._list_presence("agent")

    assert {tuple(sorted(record.items())) for record in records} == {
        (("future", "kept"), ("instance_id", "remote"), ("session_id", ""), ("user_id", "")),
        (("session_id", " session "),),
    }
    remove.assert_awaited_once_with("realtime:ws:agent:agent", "stale")
    store.pipeline.assert_called_once()
    assert read.call_count == 3


@pytest.mark.parametrize(("session_id", "user_id"), [(None, None), ("", ""), ("session", "user")])
async def test_registration_writes_string_presence_with_existing_ttl(
    monkeypatch: pytest.MonkeyPatch, session_id: str | None, user_id: str | None
) -> None:
    socket = WebSocket({"type": "websocket", "headers": []}, AsyncMock(), AsyncMock())
    pipe = MagicMock()
    write_hash = Mock()
    expire = Mock()
    pipe.hset = write_hash
    pipe.expire = expire
    pipe.__aenter__ = AsyncMock(return_value=pipe)
    pipe.__aexit__ = AsyncMock(return_value=False)
    pipe.execute = AsyncMock()
    store = SimpleNamespace(pipeline=Mock(return_value=pipe))
    monkeypatch.setattr(router, "get_redis", AsyncMock(return_value=store))
    instance = router.RealtimeRouter()
    instance.instance_id = "replica"

    connection_id = await instance.register_connection(
        agent_id="agent", websocket=socket, session_id=session_id, user_id=user_id
    )

    connection_key = f"realtime:ws:conn:{connection_id}"
    write_hash.assert_has_calls(
        [
            call(connection_key, "agent_id", "agent"),
            call(connection_key, "session_id", session_id or ""),
            call(connection_key, "user_id", user_id or ""),
            call(connection_key, "instance_id", "replica"),
        ]
    )
    assert write_hash.call_count == 4
    expire.assert_has_calls([call(connection_key, 180), call("realtime:ws:agent:agent", 180)])
    assert object_attr(socket.state, "realtime_connection_id") == connection_id


async def test_routing_is_local_first_and_publishes_once_per_matching_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    published: list[tuple[str, str]] = []
    frames: list[Message] = []

    async def receive() -> Message:
        return {"type": "websocket.connect"}

    async def send(frame: Message) -> None:
        if frame["type"] == "websocket.send":
            events.append("local")
            frames.append(frame)

    socket = WebSocket({"type": "websocket", "headers": []}, receive, send)
    await socket.accept()
    instance = router.RealtimeRouter()
    instance.instance_id = "local"

    async def authorize(
        _socket: WebSocket, _agent: str, _session: str | None, acl_version: str | None = None, *, wait_seconds: float
    ) -> DeliveryVerdict:
        assert acl_version is None  # an ordinary session reads no ACL version
        assert wait_seconds == router.DEFAULT_AUTHORIZATION_WAIT_SECONDS
        events.append("authorize")
        return DeliveryVerdict.ALLOWED

    async def presence(_agent: str) -> list[dict[str, str]]:
        events.append("presence")
        return [
            {"instance_id": identity, "session_id": session, "user_id": "user"}
            for identity, session in [
                ("local", "session"),
                ("remote", "session"),
                ("remote", "session"),
                ("archive", "other"),
                ("", "session"),
            ]
        ]

    async def publish(channel: str, payload: str) -> int:
        events.append("publish")
        published.append((channel, payload))
        return 1

    monkeypatch.setattr(router, "authorize_chat_delivery", authorize)
    monkeypatch.setattr(instance, "_list_presence", presence)
    monkeypatch.setattr(router, "get_redis", AsyncMock(return_value=SimpleNamespace(publish=publish)))
    message: dict[str, object] = {"type": "custom", "data": [None, {"nested": True}]}

    await instance.route_message(
        agent_id="agent",
        message=message,
        local_connections=[(socket, "session", "user"), (socket, "other", "user")],
        session_id="session",
        user_id="user",
    )

    # Presence runs alongside the local send; remote publication waits for both.
    assert sorted(events) == ["authorize", "local", "presence", "publish"]
    assert events.index("authorize") < events.index("local") < events.index("publish")
    assert events.index("presence") < events.index("publish")
    assert len(frames) == 1
    assert [(channel, json_loads_object(payload)) for channel, payload in published] == [
        (
            "realtime:ws:instance:remote",
            {
                "agent_id": "agent",
                "message": message,
                "session_id": "session",
                "user_id": "user",
                "origin_instance_id": "local",
            },
        )
    ]


class Peer:
    """An accepted socket that records its frames; ``stalled`` never completes a text send, ``stall_close`` a close."""

    def __init__(self, *, stalled: bool = False, stall_close: bool = False) -> None:
        self.frames: list[Message] = []
        self.closes: list[int] = []
        self.close_cancelled = False
        self.stalled = stalled
        self.stall_close = stall_close
        self.socket = WebSocket({"type": "websocket", "headers": []}, self._receive, self._send)

    async def _receive(self) -> Message:
        return {"type": "websocket.connect"}

    async def _send(self, frame: Message) -> None:
        if frame["type"] == "websocket.send" and self.stalled:
            await asyncio.Event().wait()
        if frame["type"] == "websocket.close":
            self.closes.append(frame["code"])
            if self.stall_close:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.close_cancelled = True
                    raise
        self.frames.append(frame)

    async def accepted(self) -> WebSocket:
        await self.socket.accept()
        self.frames.clear()
        return self.socket


class Observer:
    """A ``SendObserver`` that records what the router hands back and evicts on the first timeout."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, WebSocket]] = []
        self.evicted: list[WebSocket] = []
        self.refusals: list[tuple[WebSocket, int]] = []

    def is_live(self, websocket: WebSocket) -> bool:
        self.calls.append(("is_live", websocket))
        return websocket not in self.evicted

    def sent(self, websocket: WebSocket) -> None:
        self.calls.append(("sent", websocket))

    def timed_out(self, agent_id: str, websocket: WebSocket) -> bool:
        assert agent_id == "agent"
        self.calls.append(("timed_out", websocket))
        self.evicted.append(websocket)
        return True

    def refused(self, agent_id: str, websocket: WebSocket, close_code: int) -> None:
        assert agent_id == "agent"
        self.calls.append(("refused", websocket))
        self.refusals.append((websocket, close_code))


async def test_routing_returns_its_outcome_counts_remote_publish_errors_and_keeps_no_socket_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    healthy, stalled, stranger = (
        await Peer().accepted(),
        await Peer(stalled=True).accepted(),
        await Peer().accepted(),
    )
    instance = router.RealtimeRouter()
    instance.instance_id = "local"
    published: list[str] = []
    warnings: list[str] = []

    async def authorize(
        socket: WebSocket, _agent: str, _session: str | None, acl_version: str | None = None, *, wait_seconds: float
    ) -> DeliveryVerdict:
        return DeliveryVerdict.NOT_VIEWER if socket is stranger else DeliveryVerdict.ALLOWED

    async def presence(_agent: str) -> list[dict[str, str]]:
        return [{"instance_id": identity, "session_id": "session", "user_id": "user"} for identity in ("up", "down")]

    async def publish(channel: str, _payload: str) -> int:
        published.append(channel)
        if channel.endswith(":down"):
            raise ConnectionError("instance channel unavailable")
        return 1

    monkeypatch.setattr(router, "authorize_chat_delivery", authorize)
    monkeypatch.setattr(instance, "_list_presence", presence)
    monkeypatch.setattr(router, "get_redis", AsyncMock(return_value=SimpleNamespace(publish=publish)))
    monkeypatch.setattr(router, "logger", SimpleNamespace(debug=lambda _message: None, warning=warnings.append))
    observer = Observer()
    state_before = dict(vars(instance))

    outcome = await asyncio.wait_for(
        instance.route_message(
            agent_id="agent",
            message={"type": "chunk", "seq": 0},
            local_connections=[(socket, "session", "user") for socket in (healthy, stalled, stranger)],
            session_id="session",
            send_timeout=0.0,
            observer=observer,
        ),
        5,
    )

    assert outcome == RoutingOutcome(
        local_targets=3, delivered=1, failed=1, evicted=1, remote_instances=2, remote_publish_errors=1
    )
    assert outcome.dropped
    assert sorted(published) == ["realtime:ws:instance:down", "realtime:ws:instance:up"]
    assert any("ConnectionError" in warning for warning in warnings)
    # The stalled socket went back to its owner; the router remembers nothing about any socket.
    assert sorted(name for name, socket in observer.calls if socket is stalled) == ["is_live", "timed_out"]
    assert sorted(name for name, socket in observer.calls if socket is healthy) == ["is_live", "sent"]
    assert not [name for name, socket in observer.calls if socket is stranger]
    assert vars(instance) == state_before


async def test_a_skipped_socket_counts_as_dropped_and_a_revoked_one_goes_back_to_its_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    viewers = {
        DeliveryVerdict.ALLOWED: await Peer().accepted(),
        DeliveryVerdict.SKIPPED: await Peer().accepted(),
        DeliveryVerdict.UNAUTHENTICATED: await Peer().accepted(),
        DeliveryVerdict.FORBIDDEN: await Peer().accepted(),
    }
    verdicts = {socket: verdict for verdict, socket in viewers.items()}
    waits: list[float] = []
    instance = router.RealtimeRouter()
    instance.instance_id = "local"

    async def authorize(
        socket: WebSocket, _agent: str, _session: str | None, acl_version: str | None = None, *, wait_seconds: float
    ) -> DeliveryVerdict:
        waits.append(wait_seconds)
        return verdicts[socket]

    async def presence(_agent: str) -> list[dict[str, str]]:
        return []

    monkeypatch.setattr(router, "authorize_chat_delivery", authorize)
    monkeypatch.setattr(instance, "_list_presence", presence)
    observer = Observer()

    outcome = await instance.route_message(
        agent_id="agent",
        message={"type": "chunk", "seq": 0},
        local_connections=[(socket, "session", "user") for socket in viewers.values()],
        session_id="session",
        observer=observer,
    )

    assert outcome == RoutingOutcome(local_targets=4, delivered=1, skipped=1)
    assert outcome.dropped
    assert waits == [router.CHUNK_AUTHORIZATION_WAIT_SECONDS] * 4
    # The router closes nothing itself: each revoked socket goes to its owner once, with the code of its verdict.
    assert sorted(observer.refusals, key=lambda refusal: refusal[1]) == [
        (viewers[DeliveryVerdict.UNAUTHENTICATED], 4001),
        (viewers[DeliveryVerdict.FORBIDDEN], 4003),
    ]
    assert not [name for name, socket in observer.calls if socket is viewers[DeliveryVerdict.SKIPPED]]


@pytest.mark.parametrize("listener", ["none", "not_viewer", "revoked"])
async def test_an_event_nobody_may_receive_is_not_dropped(monkeypatch: pytest.MonkeyPatch, listener: str) -> None:
    instance = router.RealtimeRouter()
    instance.instance_id = "local"
    socket = await Peer().accepted()

    async def refuse(
        _socket: WebSocket, _agent: str, _session: str | None, acl_version: str | None = None, *, wait_seconds: float
    ) -> DeliveryVerdict:
        return DeliveryVerdict.FORBIDDEN if listener == "revoked" else DeliveryVerdict.NOT_VIEWER

    async def presence(_agent: str) -> list[dict[str, str]]:
        return []

    monkeypatch.setattr(router, "authorize_chat_delivery", refuse)
    monkeypatch.setattr(instance, "_list_presence", presence)

    outcome = await instance.route_message(
        agent_id="agent",
        message={"type": "chunk", "seq": 0},
        local_connections=[] if listener == "none" else [(socket, "session", "user")],
        session_id="session",
        observer=Observer(),
    )

    assert outcome == RoutingOutcome(local_targets=0 if listener == "none" else 1)
    assert not outcome.dropped


async def test_eviction_closes_once_with_its_own_code_in_a_bounded_task_the_manager_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(websocket, "EVICTION_CLOSE_TIMEOUT_SECONDS", 0.0)  # expires at the close's first suspension
    threshold = websocket.STALLED_SEND_EVICTION_THRESHOLD
    manager = websocket.ConnectionManager()
    peer = Peer(stall_close=True)
    socket = await peer.accepted()
    manager.active_connections["agent"] = [(socket, "session", "user")]

    evictions = [manager.timed_out("agent", socket) for _ in range(threshold)]

    assert evictions == [False] * (threshold - 1) + [True]
    assert not manager.is_live(socket)
    assert manager.active_connections["agent"] == []
    (task,) = manager._eviction_tasks
    assert not task.done()
    await asyncio.wait_for(manager.drain_evictions(), 5)
    assert task.done()
    assert not manager._eviction_tasks
    assert peer.closes == [websocket.EVICTION_CLOSE_CODE]
    assert peer.closes[0] not in (websocket.RESUBSCRIBE_CLOSE_CODE, 4001, 4003)
    assert peer.close_cancelled
    assert manager.timed_out("agent", socket) is False
    assert not manager._eviction_tasks


async def test_resubscribe_still_closes_every_listed_socket_with_1012_and_not_the_evicted_one() -> None:
    manager = websocket.ConnectionManager()
    live, evicted = Peer(), Peer()
    manager.active_connections["agent"] = [
        (await live.accepted(), "session", "user"),
        (await evicted.accepted(), "session", "user"),
    ]
    for _ in range(websocket.STALLED_SEND_EVICTION_THRESHOLD):
        _ = manager.timed_out("agent", evicted.socket)

    await asyncio.wait_for(manager.close_local_connections(), 5)
    await asyncio.wait_for(manager.drain_evictions(), 5)

    assert live.closes == [websocket.RESUBSCRIBE_CLOSE_CODE] == [1012]
    assert evicted.closes == [websocket.EVICTION_CLOSE_CODE]
