"""Shared-session delivery authorization is cached briefly and bound to the agent ACL version."""

import asyncio
import time
from collections.abc import Callable
from contextlib import nullcontext
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException, WebSocket
from starlette.types import Message

from app.api.websocket import ConnectionManager
from app.core import access_cache
from app.services import chat_tickets
from app.services.realtime_runtime import chat_authorization
from app.services.realtime_runtime.chat_authorization import (
    DELIVERY_AUTHORIZATION_TTL_SECONDS,
    DeliveryVerdict,
    authorize_chat_delivery,
    read_shared_acl_version,
)
from app.services.realtime_runtime.fanout import LOCAL_FANOUT_CONCURRENCY
from app.services.realtime_runtime.router import RealtimeRouter
from app.services.shared_chat import shared_session_id
from test_chunk_live_delivery import TRANSIENT_FAILURES, Gate

ALLOWED = DeliveryVerdict.ALLOWED
FORBIDDEN = DeliveryVerdict.FORBIDDEN
# Long enough that every decision here settles inside it; the wait bounds are covered in test_chunk_live_delivery.
WAIT_SECONDS = 60.0


class Clock:
    def __init__(self) -> None:
        self.now = 50.0

    def __call__(self) -> float:
        return self.now


class Env:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.clock = Clock()
        self.version = "1"
        self.authorize = AsyncMock(return_value=(object(), object(), "use"))
        self.authorized_session = AsyncMock()
        self.read_version = AsyncMock(side_effect=lambda _agent_id: self.version)
        monkeypatch.setattr(chat_authorization, "_clock", self.clock)
        monkeypatch.setattr(chat_authorization, "connection_ctx", nullcontext)
        monkeypatch.setattr(access_cache, "read_acl_version", self.read_version)
        monkeypatch.setattr(chat_tickets, "authorize_identity", self.authorize)
        monkeypatch.setattr(chat_tickets, "authorized_session", self.authorized_session)

    def revoke(self) -> None:
        self.authorize.side_effect = HTTPException(403, "Permission revoked")


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Env:
    return Env(monkeypatch)


def shared_socket(frames: list[Message] | None = None) -> tuple[WebSocket, str, str]:
    agent_id = uuid4()
    sink = frames if frames is not None else []

    async def receive() -> Message:
        return {"type": "websocket.connect"}

    async def send(message: Message) -> None:
        sink.append(message)

    socket = WebSocket({"type": "websocket", "headers": []}, receive, send)
    socket.state.chat_binding = chat_tickets.ChatTicketBinding(
        user_id=uuid4(),
        tenant_id=uuid4(),
        agent_id=agent_id,
        session_id=shared_session_id(agent_id),
        auth_deadline=int(time.time()) + 3600,
        auth_generation=0,
    )
    return socket, str(agent_id), str(shared_session_id(agent_id))


async def check(
    socket: WebSocket, agent_id: str, session_id: str | None, acl_version: str | None = None
) -> DeliveryVerdict:
    return await authorize_chat_delivery(socket, agent_id, session_id, acl_version, wait_seconds=WAIT_SECONDS)


async def test_positive_result_is_reused_within_ttl_then_expires(env: Env) -> None:
    socket, agent_id, session_id = shared_socket()

    assert [await check(socket, agent_id, session_id) for _ in range(3)] == [ALLOWED] * 3
    env.authorize.assert_awaited_once()
    env.authorized_session.assert_awaited_once()

    env.clock.now += DELIVERY_AUTHORIZATION_TTL_SECONDS - 0.1
    assert await check(socket, agent_id, session_id) is ALLOWED
    env.authorize.assert_awaited_once()

    env.clock.now += 0.1
    assert await check(socket, agent_id, session_id) is ALLOWED
    assert env.authorize.await_count == 2


async def test_acl_version_change_forces_reauthorization(env: Env) -> None:
    socket, agent_id, session_id = shared_socket()
    assert await check(socket, agent_id, session_id) is ALLOWED

    env.version = "2"
    assert await check(socket, agent_id, session_id) is ALLOWED
    assert env.authorize.await_count == 2

    assert await check(socket, agent_id, session_id) is ALLOWED
    assert env.authorize.await_count == 2


async def test_cache_is_per_socket(env: Env) -> None:
    first, agent_id, session_id = shared_socket()
    second, _, _ = shared_socket()
    second.state.chat_binding = first.state.chat_binding.model_copy()

    assert await check(first, agent_id, session_id) is ALLOWED
    assert await check(second, agent_id, session_id) is ALLOWED

    assert env.authorize.await_count == 2


async def test_negative_results_are_never_cached(env: Env) -> None:
    socket, agent_id, session_id = shared_socket()
    env.revoke()

    assert await check(socket, agent_id, session_id) is FORBIDDEN
    assert await check(socket, agent_id, session_id) is FORBIDDEN
    assert env.authorize.await_count == 2

    env.authorize.side_effect = None
    assert await check(socket, agent_id, session_id) is ALLOWED
    assert await check(socket, agent_id, session_id) is ALLOWED
    assert env.authorize.await_count == 3


async def test_failed_reauthorization_discards_the_previous_positive_result(env: Env) -> None:
    socket, agent_id, session_id = shared_socket()
    assert await check(socket, agent_id, session_id) is ALLOWED

    env.version = "2"
    env.revoke()
    assert await check(socket, agent_id, session_id) is FORBIDDEN

    env.version = "1"
    assert await check(socket, agent_id, session_id) is FORBIDDEN
    assert env.authorize.await_count == 3


async def test_non_shared_target_is_allowed_without_any_work(env: Env) -> None:
    socket, agent_id, _ = shared_socket()

    assert await check(socket, agent_id, str(uuid4())) is ALLOWED
    assert await check(socket, agent_id, None) is ALLOWED

    env.read_version.assert_not_awaited()
    env.authorize.assert_not_awaited()


async def test_supplied_acl_version_replaces_the_per_socket_read(env: Env) -> None:
    socket, agent_id, session_id = shared_socket()

    assert await check(socket, agent_id, session_id, acl_version="1") is ALLOWED
    assert await check(socket, agent_id, session_id, acl_version="1") is ALLOWED
    env.authorize.assert_awaited_once()

    # A bumped version re-authorizes on the very next event; a negative result is still never cached.
    env.revoke()
    assert await check(socket, agent_id, session_id, acl_version="2") is FORBIDDEN
    assert await check(socket, agent_id, session_id, acl_version="2") is FORBIDDEN
    assert env.authorize.await_count == 3
    env.read_version.assert_not_awaited()


async def test_delivery_acl_version_is_read_once_and_only_for_a_shared_target(env: Env) -> None:
    _, agent_id, session_id = shared_socket()
    env.version = "9"

    assert await read_shared_acl_version(agent_id, [None, str(uuid4()), session_id, session_id]) == "9"
    env.read_version.assert_awaited_once()

    env.read_version.reset_mock()
    assert await read_shared_acl_version(agent_id, [None, str(uuid4())]) is None
    env.read_version.assert_not_awaited()


@pytest.mark.parametrize(
    ("failure", "verdict"),
    [
        (lambda: HTTPException(401, "Authentication expired"), DeliveryVerdict.UNAUTHENTICATED),
        (lambda: HTTPException(403, "Permission revoked"), DeliveryVerdict.FORBIDDEN),
        (lambda: HTTPException(404, "Agent not found"), DeliveryVerdict.FORBIDDEN),
        *((make, DeliveryVerdict.SKIPPED) for make in TRANSIENT_FAILURES.values()),
        (lambda: RuntimeError("unexpected"), DeliveryVerdict.SKIPPED),
    ],
    ids=["401", "403", "404", *TRANSIENT_FAILURES, "unexpected"],
)
async def test_only_401_403_and_404_revoke_and_no_failure_is_cached(
    env: Env, failure: Callable[[], Exception], verdict: DeliveryVerdict
) -> None:
    socket, agent_id, session_id = shared_socket()
    env.authorize.side_effect = failure()

    assert [await check(socket, agent_id, session_id) for _ in range(2)] == [verdict] * 2

    assert env.authorize.await_count == 2
    assert socket.state.chat_delivery_authorization is None
    assert (
        verdict.close_code
        == {
            DeliveryVerdict.UNAUTHENTICATED: 4001,
            DeliveryVerdict.FORBIDDEN: 4003,
            DeliveryVerdict.SKIPPED: None,
        }[verdict]
    )


async def test_cold_decisions_never_exceed_the_fanout_bound_across_deliveries(env: Env) -> None:
    bound = LOCAL_FANOUT_CONCURRENCY
    gate = Gate(saturate_at=bound)

    async def held(_binding: chat_tickets.ChatTicketBinding) -> tuple[object, object, str]:
        await gate.pass_through()
        return object(), object(), "use"

    env.authorize.side_effect = held
    # Separate viewers, each its own decision, checked side by side as concurrent deliveries would.
    sockets = [shared_socket() for _ in range(bound + 4)]
    checks = asyncio.gather(*(check(*socket) for socket in sockets))
    await asyncio.wait_for(gate.saturated.wait(), 5)

    assert gate.in_flight == bound
    gate.open.set()
    assert await asyncio.wait_for(checks, 5) == [ALLOWED] * len(sockets)
    assert gate.peak == bound
    assert env.authorize.await_count == len(sockets)


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("bumped", [True, False])
async def test_revoked_viewer_stops_receiving_after_acl_bump(
    monkeypatch: pytest.MonkeyPatch, env: Env, bumped: bool, remote: bool
) -> None:
    frames: list[Message] = []
    socket, agent_id, session_id = shared_socket(frames)
    await socket.accept()
    frames.clear()
    instance = RealtimeRouter()
    monkeypatch.setattr(instance, "_list_presence", AsyncMock(return_value=[]))
    manager = ConnectionManager()
    manager.active_connections[agent_id] = [(socket, session_id, str(socket.state.chat_binding.user_id))]

    async def send() -> None:
        if remote:
            await manager.deliver_pubsub_message(agent_id=agent_id, session_id=session_id, payload={"type": "done"})
        else:
            await instance.route_message(
                agent_id=agent_id,
                session_id=session_id,
                message={"type": "done"},
                local_connections=manager.active_connections[agent_id],
                observer=manager,
            )
        await manager.drain_evictions()

    def sends() -> int:
        return sum(frame["type"] == "websocket.send" for frame in frames)

    def closes() -> list[int]:
        return [frame["code"] for frame in frames if frame["type"] == "websocket.close"]

    await send()
    assert sends() == 1

    env.revoke()
    if bumped:
        env.version = "2"
    await send()

    # Unbumped revocations only lapse once the 5 s window ends; then the socket closes like a bumped one.
    assert (sends(), closes()) == ((1, [4003]) if bumped else (2, []))
    if not bumped:
        env.clock.now += DELIVERY_AUTHORIZATION_TTL_SECONDS
        await send()
        assert (sends(), closes()) == (2, [4003])
    assert manager.active_connections[agent_id] == []
