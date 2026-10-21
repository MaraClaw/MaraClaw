import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, call

import pytest
from fastapi import WebSocket
from starlette.types import Message

from app.core.json_types import json_loads_object, object_attr
from app.services.realtime_runtime import router
from app.services.realtime_runtime.contracts import LocalDeliverer


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
    store = SimpleNamespace(
        smembers=AsyncMock(return_value={b"partial", "session", "stale"}),
        hgetall=AsyncMock(side_effect=hashes.__getitem__),
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

    async def authorize(_socket: WebSocket, _agent: str, _session: str | None) -> bool:
        events.append("authorize")
        return True

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

    assert events == ["authorize", "local", "presence", "publish"]
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
