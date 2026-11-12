"""Connection ownership and error reporting must survive unavailable transports."""

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal, assert_never
from unittest.mock import AsyncMock, Mock

import anyio
import pytest
from anyio.lowlevel import checkpoint
from fastapi import FastAPI, HTTPException, WebSocket
from redis.asyncio import Redis
from redis.asyncio.client import Pipeline
from redis.exceptions import TimeoutError as RedisTimeoutError
from starlette.types import Message, Scope

from app.api import websocket
from app.records.agent import AgentRecord
from app.records.chat import ChatMessageRecord
from app.records.user import UserRecord
from app.services import chat_tickets
from app.services.chat_submissions import AcceptedSubmission
from app.services.realtime_runtime import router as realtime

type Lifecycle = tuple[chat_tickets.ChatTicketBinding, websocket.ConnectionManager, AsyncMock]


@pytest.fixture
async def lifecycle(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Lifecycle]:
    binding = chat_tickets.ChatTicketBinding(
        user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        auth_deadline=int(time.time()) + 3600,
        auth_generation=7,
    )
    user = UserRecord(id=binding.user_id, tenant_id=binding.tenant_id, display_name="Member")
    agent = AgentRecord(id=binding.agent_id, creator_id=user.id, name="Agent")
    manager = websocket.ConnectionManager()
    execute = AsyncMock(return_value=[])
    monkeypatch.setattr(websocket, "manager", manager)
    monkeypatch.setattr(websocket, "realtime_router", realtime.RealtimeRouter())
    monkeypatch.setattr(chat_tickets, "redeem", AsyncMock(return_value=binding))
    monkeypatch.setattr(chat_tickets, "authorize", AsyncMock(return_value=(user, agent, "use")))
    monkeypatch.setattr(chat_tickets, "authorized_session", AsyncMock())
    monkeypatch.setattr(websocket.chat_message_dao, "list_recent", AsyncMock(return_value=[]))
    # Keep Redis's real pipeline and command queue; replace only the network execution.
    monkeypatch.setattr(Pipeline, "execute", execute)
    async with Redis() as redis:
        monkeypatch.setattr(realtime, "get_redis", AsyncMock(return_value=redis))
        yield binding, manager, execute


def peer_socket(sent: list[Message], *, broken_send: bool = False, frames: tuple[Message, ...] = ()) -> WebSocket:
    incoming: list[Message] = [
        {"type": "websocket.connect"},
        *frames,
        {"type": "websocket.disconnect", "code": 1000},
    ]
    messages = iter(incoming)

    async def receive() -> Message:
        return next(messages)

    async def send(message: Message) -> None:
        sent.append(message)
        if broken_send and message["type"] != "websocket.accept":
            # Starlette, not this test, converts this ASGI failure to disconnect 1006.
            raise OSError("peer transport is already closed")

    return WebSocket({"type": "websocket", "headers": []}, receive, send)


@pytest.mark.parametrize("failure", ["http-error", "auth-timeout", "redis-timeout"])
async def test_peer_disconnect_during_error_reporting_is_contained(
    lifecycle: Lifecycle,
    monkeypatch: pytest.MonkeyPatch,
    failure: Literal["http-error", "auth-timeout", "redis-timeout"],
) -> None:
    # Given a real socket whose transport fails only after the handshake.
    binding, manager, execute = lifecycle
    sent: list[Message] = []
    ws = peer_socket(sent, broken_send=True)
    match failure:
        case "http-error":
            monkeypatch.setattr(chat_tickets, "redeem", AsyncMock(side_effect=HTTPException(503, "unavailable")))
            expected_frame = "websocket.send"
            expected_code = None
        case "auth-timeout":
            monkeypatch.setattr(chat_tickets, "authorize", AsyncMock(side_effect=TimeoutError("auth deadline")))
            expected_frame = "websocket.close"
            expected_code = 4001
        case "redis-timeout":
            execute.side_effect = RedisTimeoutError("presence registration timeout")
            expected_frame = "websocket.close"
            expected_code = 1011
        case unreachable:
            assert_never(unreachable)
    handler = websocket.WebSocketChatHandler(ws, binding.agent_id, "opaque", str(binding.session_id))

    # When reporting the original error encounters Starlette's disconnect 1006.
    await handler.run()

    # Then the secondary transport failure is contained and local ownership is released.
    assert len(sent) == 2
    assert sent[-1]["type"] == expected_frame
    assert sent[-1].get("code") == expected_code
    assert manager.active_connections.get(str(binding.agent_id), []) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("history_failed", [False, True])
async def test_welcome_requires_successfully_loaded_empty_history(
    lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch, history_failed: bool
) -> None:
    binding, _manager, _execute = lifecycle
    user = UserRecord(id=binding.user_id, tenant_id=binding.tenant_id)
    agent = AgentRecord(id=binding.agent_id, creator_id=user.id, name="Agent", welcome_message="Welcome")
    monkeypatch.setattr(chat_tickets, "authorize", AsyncMock(return_value=(user, agent, "use")))
    history = AsyncMock(return_value=[], side_effect=RuntimeError("history unavailable") if history_failed else None)
    monkeypatch.setattr(websocket.chat_message_dao, "list_recent", history)
    report = Mock()
    monkeypatch.setattr(websocket, "report_exception", report)
    sent: list[Message] = []
    handler = websocket.WebSocketChatHandler(peer_socket(sent), binding.agent_id, "opaque", str(binding.session_id))

    await handler.run()

    events = [json.loads(frame["text"]) for frame in sent if frame["type"] == "websocket.send"]
    assert [event["type"] for event in events] == (["connected"] if history_failed else ["connected", "done"])
    assert not any(frame["type"] == "websocket.close" for frame in sent)
    assert report.call_count == int(history_failed)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ["not json", "[]", "null", '"text"', "42", b"binary"])
async def test_malformed_frame_reports_error_and_next_submission_is_processed(
    lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch, payload: str | bytes
) -> None:
    binding, _manager, _execute = lifecycle
    submission_id = str(uuid.uuid4())
    message = ChatMessageRecord(
        id=uuid.uuid4(), agent_id=binding.agent_id, user_id=binding.user_id, role="user", content="hello"
    )
    accept = AsyncMock(return_value=AcceptedSubmission(message=message, replayed=True))
    monkeypatch.setattr(websocket, "accept_submission", accept)
    report = Mock()
    monkeypatch.setattr(websocket, "report_exception", report)
    malformed: Message = {"type": "websocket.receive"}
    malformed["bytes" if isinstance(payload, bytes) else "text"] = payload
    valid: Message = {
        "type": "websocket.receive",
        "text": json.dumps({"content": "hello", "client_submission_id": submission_id}),
    }
    sent: list[Message] = []
    handler = websocket.WebSocketChatHandler(
        peer_socket(sent, frames=(malformed, valid)), binding.agent_id, "opaque", str(binding.session_id)
    )

    await handler.run()

    events = [json.loads(frame["text"]) for frame in sent if frame["type"] == "websocket.send"]
    assert [event["type"] for event in events] == ["connected", "error", "turn_accepted"]
    assert isinstance(events[1]["content"], str)
    assert events[2]["client_submission_id"] == submission_id
    accept.assert_awaited_once()
    assert accept.call_args.args[0].content == "hello"
    assert not any(frame["type"] == "websocket.close" for frame in sent)
    report.assert_not_called()


async def test_redis_unregister_failure_is_contained_after_peer_disconnect(lifecycle: Lifecycle) -> None:
    # Given successful presence registration followed by a Redis failure during cleanup.
    binding, manager, execute = lifecycle
    execute.side_effect = [[], RedisTimeoutError("presence unregister timeout")]
    sent: list[Message] = []
    ws = peer_socket(sent)
    handler = websocket.WebSocketChatHandler(ws, binding.agent_id, "opaque", str(binding.session_id))

    # When the peer disconnects normally, the real manager and router perform cleanup.
    await handler.run()

    # Then Redis's secondary failure does not escape the socket lifecycle.
    assert execute.await_count == 2
    assert manager.active_connections.get(str(binding.agent_id), []) == []
    assert isinstance(sent[1]["text"], str)
    assert json.loads(sent[1]["text"])["type"] == "connected"


async def test_failed_redis_registration_never_publishes_local_socket(lifecycle: Lifecycle) -> None:
    # Given a real manager whose Redis registration pipeline times out.
    binding, manager, execute = lifecycle
    execute.side_effect = RedisTimeoutError("presence registration timeout")
    sent: list[Message] = []
    ws = peer_socket(sent)

    # When registration fails, the caller still receives the original Redis failure.
    with pytest.raises(RedisTimeoutError, match="presence registration timeout"):
        await manager.connect(str(binding.agent_id), ws, str(binding.session_id), str(binding.user_id))

    # Then an unregistered socket cannot be discovered by local delivery.
    assert manager.active_connections.get(str(binding.agent_id), []) == []


async def test_healthy_registration_and_peer_disconnect_release_local_socket(lifecycle: Lifecycle) -> None:
    # Given healthy Redis presence and a peer that disconnects after setup.
    binding, manager, execute = lifecycle
    sent: list[Message] = []
    ws = peer_socket(sent)
    handler = websocket.WebSocketChatHandler(ws, binding.agent_id, "opaque", str(binding.session_id))

    # When the real lifecycle runs through setup and peer disconnect.
    await handler.run()

    # Then the connected event and both Redis operations survive unchanged.
    assert sent[0]["type"] == "websocket.accept"
    assert isinstance(sent[1]["text"], str)
    assert json.loads(sent[1]["text"]) == {"type": "connected", "session_id": str(binding.session_id)}
    assert len(sent) == 2
    assert execute.await_count == 2
    assert manager.active_connections.get(str(binding.agent_id), []) == []


chat_app = FastAPI()
chat_app.include_router(websocket.router)
DISCONNECT: Message = {"type": "websocket.disconnect", "code": 1000}


class Browser:
    """An open tab: the test feeds it frames and reads what the server sent, in order."""

    def __init__(self, heartbeat_send: Literal["ok", "error", "stall"] = "ok") -> None:
        self.heartbeat_send: Literal["ok", "error", "stall"] = heartbeat_send
        self.inbound: asyncio.Queue[Message] = asyncio.Queue()
        self.events: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        self.delivered: list[str] = []
        self.closes: list[int] = []
        self.handshaken = False

    async def receive(self) -> Message:
        if not self.handshaken:
            self.handshaken = True
            return {"type": "websocket.connect"}
        return await self.inbound.get()

    async def send(self, message: Message) -> None:
        if message["type"] == "websocket.close":
            self.closes.append(message["code"])
            self.inbound.put_nowait({"type": "websocket.disconnect", "code": message["code"]})
        elif message["type"] == "websocket.send":
            event = json.loads(message["text"])
            if event["type"] == "heartbeat":
                match self.heartbeat_send:
                    case "error":
                        raise RuntimeError("heartbeat transport failure")
                    case "stall":
                        await asyncio.Event().wait()
                    case "ok":
                        pass
                    case unreachable:
                        assert_never(unreachable)
            await checkpoint()  # a real transport suspends while it writes
            self.delivered.append(str(event["type"]))
            self.events.put_nowait(event)

    async def next_event(self) -> dict[str, object]:
        async with asyncio.timeout(5):
            return await self.events.get()

    def drain(self) -> list[dict[str, object]]:
        return [self.events.get_nowait() for _ in range(self.events.qsize())]


class Cadence:
    """Stands in for the heartbeat sleep: each tick lets exactly one pending wait elapse."""

    def __init__(self, browser: Browser) -> None:
        self.browser = browser
        self.intervals: list[float] = []
        self.delivered_at_first_wait: list[str] | None = None
        self.cancelled = asyncio.Event()
        self._parked = asyncio.Event()
        self._elapsed: asyncio.Queue[None] = asyncio.Queue()

    async def sleep(self, seconds: float) -> None:
        if not self.intervals:
            self.delivered_at_first_wait = list(self.browser.delivered)
        self.intervals.append(seconds)
        self._parked.set()
        try:
            await self._elapsed.get()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise

    async def parked(self) -> None:
        async with asyncio.timeout(5):
            await self._parked.wait()

    async def tick(self) -> None:
        await self.parked()
        self._parked.clear()
        self._elapsed.put_nowait(None)


@dataclass
class Session:
    """One chat socket on the real route, with the heartbeat clock in the test's hands."""

    browser: Browser
    cadence: Cadence
    run: asyncio.Task[None]
    execute: AsyncMock
    authorize: AsyncMock
    authorized_session: AsyncMock
    history: AsyncMock
    redis_runs_after_sender_cancel: list[bool]

    async def beat(self) -> None:
        await self.cadence.tick()
        await self.cadence.parked()

    async def finish(self) -> None:
        async with asyncio.timeout(5):
            await self.run

    def io_calls(self) -> tuple[int, int, int, int]:
        return (
            self.execute.await_count,
            self.authorize.await_count,
            self.authorized_session.await_count,
            self.history.await_count,
        )


async def open_session(
    lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch, *, heartbeat: bool = True, browser: Browser | None = None
) -> Session:
    binding, _manager, execute = lifecycle
    browser = browser or Browser()
    cadence = Cadence(browser)
    redis_runs_after_sender_cancel: list[bool] = []

    async def run_pipeline(*_args: object, **_kwargs: object) -> list[object]:
        redis_runs_after_sender_cancel.append(cadence.cancelled.is_set())
        return []

    execute.side_effect = run_pipeline
    user = UserRecord(id=binding.user_id, tenant_id=binding.tenant_id, display_name="Member")
    agent = AgentRecord(id=binding.agent_id, creator_id=user.id, name="Agent")
    authorize = AsyncMock(return_value=(user, agent, "use"))
    authorized_session = AsyncMock()
    history = AsyncMock(return_value=[])
    monkeypatch.setattr(chat_tickets, "authorize", authorize)
    monkeypatch.setattr(chat_tickets, "authorized_session", authorized_session)
    monkeypatch.setattr(websocket.chat_message_dao, "list_recent", history)
    monkeypatch.setattr(websocket, "_heartbeat_sleep", cadence.sleep)
    monkeypatch.setattr(websocket, "report_exception", Mock())
    query = f"ticket=opaque&session_id={binding.session_id}" + ("&heartbeat=1" if heartbeat else "")
    scope: Scope = {
        "type": "websocket",
        "path": f"/ws/chat/{binding.agent_id}",
        "query_string": query.encode(),
        "headers": [],
    }
    run = asyncio.create_task(chat_app(scope, browser.receive, browser.send))
    session = Session(
        browser, cadence, run, execute, authorize, authorized_session, history, redis_runs_after_sender_cancel
    )
    assert (await browser.next_event())["type"] == "connected"
    return session


def socket_tasks() -> dict[str, asyncio.Task[object]]:
    """The live per-socket background tasks, by name: the presence refresh and the heartbeat sender."""
    return {task.get_name(): task for task in asyncio.all_tasks() if task.get_name().startswith("ws-")}


def submission_frame() -> Message:
    content = {"content": "hello", "client_submission_id": str(uuid.uuid4())}
    return {"type": "websocket.receive", "text": json.dumps(content)}


def accept_submissions(binding: chat_tickets.ChatTicketBinding, monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    message = ChatMessageRecord(
        id=uuid.uuid4(), agent_id=binding.agent_id, user_id=binding.user_id, role="user", content="hello"
    )
    accept = AsyncMock(return_value=AcceptedSubmission(message=message, replayed=True))
    monkeypatch.setattr(websocket, "accept_submission", accept)
    return accept


@pytest.fixture
def deadline_scopes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[float | None, anyio.CancelScope]]:
    scopes: list[tuple[float | None, anyio.CancelScope]] = []
    real_fail_after = anyio.fail_after

    @contextmanager
    def recording_fail_after(delay: float | None) -> Generator[anyio.CancelScope]:
        with real_fail_after(delay) as scope:
            scopes.append((delay, scope))
            yield scope

    monkeypatch.setattr(websocket.anyio, "fail_after", recording_fail_after)
    return scopes


async def test_heartbeat_is_not_scheduled_until_connected_has_been_delivered(
    lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given an opted-in socket whose transport suspends while it writes.
    session = await open_session(lifecycle, monkeypatch)

    # When the sender first asks the clock for its interval.
    await session.cadence.parked()

    # Then the connected event was already out, and nothing preceded it.
    assert session.cadence.delivered_at_first_wait == ["connected"]


async def test_opted_in_socket_gets_one_payload_free_heartbeat_per_cadence_tick(
    lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given an opted-in socket.
    session = await open_session(lifecycle, monkeypatch)

    # When three cadence intervals elapse.
    for _ in range(3):
        await session.beat()

    # Then each tick produced exactly one bare heartbeat, and every wait asked for the fixed interval.
    assert session.browser.drain() == [{"type": "heartbeat"}] * 3
    assert session.cadence.intervals == [websocket.HEARTBEAT_INTERVAL_SECONDS] * 4
    assert websocket.HEARTBEAT_INTERVAL_SECONDS == 10.0  # the web-e liveness deadline is built on this cadence


async def test_socket_without_the_flag_never_receives_a_heartbeat(
    lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a page built before the heartbeat existed: its URL carries no opt-in.
    session = await open_session(lifecycle, monkeypatch, heartbeat=False)

    # When the loop has served a frame, so a sender would long since have asked for its first interval.
    session.browser.inbound.put_nowait({"type": "websocket.receive", "text": "not json"})
    assert (await session.browser.next_event())["type"] == "error"
    session.browser.inbound.put_nowait(DISCONNECT)
    await session.finish()

    # Then nothing was scheduled and nothing but real events was sent.
    assert session.cadence.intervals == []
    assert session.browser.delivered == ["connected", "error"]


async def test_heartbeats_make_no_database_redis_or_authorization_call_and_never_reject_a_turn(
    lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a connected, opted-in socket and the I/O its setup performed.
    session = await open_session(lifecycle, monkeypatch)
    await session.cadence.parked()
    setup_calls = session.io_calls()

    # When three heartbeats are sent.
    for _ in range(3):
        await session.beat()

    # Then they added no Redis, history or authorization call, and the wire shows only heartbeats.
    assert session.io_calls() == setup_calls
    assert [event["type"] for event in session.browser.drain()] == ["heartbeat"] * 3


async def test_auth_deadline_still_closes_with_4001_while_heartbeats_flow(
    lifecycle: Lifecycle,
    monkeypatch: pytest.MonkeyPatch,
    deadline_scopes: list[tuple[float | None, anyio.CancelScope]],
) -> None:
    # Given heartbeats flowing under the deadline armed from the ticket.
    binding, _manager, _execute = lifecycle
    session = await open_session(lifecycle, monkeypatch)
    delay, scope = deadline_scopes[0]
    armed = scope.deadline
    for _ in range(2):
        await session.beat()

    # Then they neither replaced nor moved that deadline.
    assert len(deadline_scopes) == 1
    assert scope.deadline == armed
    assert delay == pytest.approx(binding.auth_deadline - time.time(), abs=5)
    assert [event["type"] for event in session.browser.drain()] == ["heartbeat"] * 2

    # When the deadline arrives.
    scope.deadline = anyio.current_time()
    await session.finish()

    # Then the socket closes with 4001 and the sender goes with it.
    assert session.browser.closes == [4001]
    assert socket_tasks() == {}


@pytest.mark.parametrize("end", ["peer-disconnect", "handler-error"])
async def test_heartbeat_sender_stops_with_the_presence_refresh_before_unregister_however_the_socket_ends(
    lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch, end: Literal["peer-disconnect", "handler-error"]
) -> None:
    # Given a socket with both background tasks running.
    session = await open_session(lifecycle, monkeypatch)
    await session.beat()
    assert set(socket_tasks()) == {"ws-heartbeat", "ws-presence-refresh"}

    # When the peer leaves, or the handler dies on an unexpected error.
    match end:
        case "peer-disconnect":
            session.browser.inbound.put_nowait(DISCONNECT)
            expected_closes: list[int] = []
        case "handler-error":
            session.authorize.side_effect = RuntimeError("authorization backend failed")
            session.browser.inbound.put_nowait(submission_frame())
            expected_closes = [1011]
        case unreachable:
            assert_never(unreachable)
    await session.finish()

    # Then no task outlives the handler, and the sender was cancelled before presence was unregistered.
    assert session.browser.closes == expected_closes
    assert socket_tasks() == {}
    assert session.cadence.cancelled.is_set()
    assert session.redis_runs_after_sender_cancel == [False, True]


@pytest.mark.parametrize("failure", ["error", "stall"])
async def test_failed_heartbeat_send_stops_only_the_sender_and_the_socket_keeps_serving(
    lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch, failure: Literal["error", "stall"]
) -> None:
    # Given a peer whose heartbeat writes fail, or stall past their send bound.
    binding, _manager, _execute = lifecycle
    accept = accept_submissions(binding, monkeypatch)
    if failure == "stall":
        monkeypatch.setattr(websocket, "DEFAULT_SEND_TIMEOUT_SECONDS", 0.0)
    session = await open_session(lifecycle, monkeypatch, browser=Browser(heartbeat_send=failure))
    await session.cadence.parked()
    sender = socket_tasks()["ws-heartbeat"]

    # When the first heartbeat is due.
    await session.cadence.tick()
    async with asyncio.timeout(5):
        await sender  # a failure that escaped the sender would be raised here

    # Then only the sender ended, and the receive loop still serves a real submission.
    assert not sender.cancelled()
    assert set(socket_tasks()) == {"ws-presence-refresh"}
    assert session.cadence.intervals == [websocket.HEARTBEAT_INTERVAL_SECONDS]
    session.browser.inbound.put_nowait(submission_frame())
    assert (await session.browser.next_event())["type"] == "turn_accepted"
    accept.assert_awaited_once()
    assert session.browser.closes == []
    session.browser.inbound.put_nowait(DISCONNECT)
    await session.finish()


@pytest.mark.parametrize("access", ["granted", "revoked"])
async def test_real_submission_during_heartbeats_is_authorized_exactly_as_before(
    lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch, access: Literal["granted", "revoked"]
) -> None:
    # Given a socket that has already heartbeated and authorized only its setup.
    binding, _manager, _execute = lifecycle
    accept = accept_submissions(binding, monkeypatch)
    session = await open_session(lifecycle, monkeypatch)
    await session.beat()
    assert session.browser.drain() == [{"type": "heartbeat"}]
    assert session.authorize.await_count == 1

    # When a real submission arrives.
    match access:
        case "granted":
            session.browser.inbound.put_nowait(submission_frame())
            assert (await session.browser.next_event())["type"] == "turn_accepted"
            accept.assert_awaited_once()
            await session.beat()
            assert session.browser.drain() == [{"type": "heartbeat"}]
            session.browser.inbound.put_nowait(DISCONNECT)
        case "revoked":
            session.authorize.side_effect = HTTPException(403, "access revoked")
            session.browser.inbound.put_nowait(submission_frame())
        case unreachable:
            assert_never(unreachable)
    await session.finish()

    # Then that frame, and only that frame, paid the authorization check, and a revoked grant still ends the socket.
    assert session.authorize.await_count == 2
    assert session.browser.closes == ([4003] if access == "revoked" else [])
    assert accept.await_count == (0 if access == "revoked" else 1)
