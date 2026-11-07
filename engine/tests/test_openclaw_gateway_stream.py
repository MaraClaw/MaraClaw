import asyncio
import json
import sys
from contextlib import contextmanager, suppress
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pywhales import DockerClient

from app.api.websocket import manager
from app.config import get_settings
from app.records.chat import ChatSessionRecord
from app.records.gateway_message import GatewayMessageRecord
from app.services import (
    openclaw_gateway_delivery as delivery,
    openclaw_gateway_rpc as rpc,
    openclaw_gateway_stream as streaming,
)
from app.services.shared_chat import shared_session_id
from app.services.turn_timing import TurnTimeline
from test_openclaw_gateway_coalescing import IngestProbe, delta_line
from test_openclaw_gateway_rpc import _Turn, wire_terminal

REF = "openrouter/nvidia/nemotron-3.5-super-vl:free"
BRAND = get_settings().APP_NAME


@pytest.mark.parametrize("shared", [False, True])
async def test_web_chunks_arrive_in_order_before_authoritative_done(monkeypatch, tmp_path, shared):
    turn = _Turn(monkeypatch, tmp_path)
    turn.message.sender_user_id = turn.agent.creator_id
    turn.message.conversation_id = str(shared_session_id(turn.agent.id) if shared else uuid4())
    session = ChatSessionRecord(uuid4(), turn.agent.id, turn.agent.creator_id)
    timeline = TurnTimeline(turn.message.id, turn.agent.id, None)
    events = []
    release = Event()

    async def get_session(_id):
        return session

    async def absent(_id):
        return None

    async def has_im(_id):
        return False

    async def send(agent_id, conversation_id, event, *, user_id, send_timeout=None):
        if event["type"] == "chunk":
            assert send_timeout == delivery.CHUNK_SEND_TIMEOUT_SECONDS
        assert agent_id == str(turn.agent.id)
        assert conversation_id == turn.message.conversation_id
        assert user_id == (None if shared else str(turn.agent.creator_id))
        events.append(event)
        if event["type"] == "chunk" and event["content"] == "replacement":
            release.set()

    @contextmanager
    def execute(_container, argv, *, stream, timeout):
        assert stream is True
        assert timeout == 940
        payload = json.loads((tmp_path / argv[-1].split("/")[-1]).read_text())
        assert payload["version"] == 2
        assert payload["stream"] is True

        def chunks():
            first = json.dumps({"kind": "delta", "messageId": turn.message_id, "seq": 0, "text": "a"})
            second = json.dumps(
                {"kind": "delta", "messageId": turn.message_id, "seq": 2, "text": "replacement", "replace": True}
            )
            data = (first + "\nnot json\n" + second + "\n").encode()
            yield "stdout", data[:17]
            yield "stderr", b"ignored diagnostic\n"
            yield "stdout", data[17:]
            assert release.wait(5), "chunks must reach the socket while exec is still running"
            yield "stdout", wire_terminal(turn.message_id).encode()

        yield chunks()

    monkeypatch.setattr(streaming.chat_session_dao, "get", get_session)
    monkeypatch.setattr(streaming.personal_burst_dispatch_dao, "get", absent)
    monkeypatch.setattr(streaming.gateway_report_delivery_dao, "has_im_origin", has_im)
    monkeypatch.setattr(manager, "send_to_session", send)
    monkeypatch.setattr(rpc, "_execute", lambda *_: pytest.fail("web must use streaming exec"))
    monkeypatch.setattr(rpc, "_policy", streaming.EXEC_POLICY)
    monkeypatch.setattr(
        "app.services.agent_manager.agent_manager.docker_client",
        SimpleNamespace(
            container=SimpleNamespace(
                execute=execute, inspect=lambda _: SimpleNamespace(state=SimpleNamespace(running=True))
            )
        ),
    )

    outcome = await asyncio.wait_for(rpc.execute_gateway_turn(turn.agent, turn.message, timing=timeline), 5)
    assert isinstance(outcome, rpc.TerminalOutcome)
    assistant_id = uuid4()
    turn.message.result = outcome.text
    await delivery.publish_report(turn.agent, delivery.RecordedReport(turn.message, assistant_id))

    assert [event["type"] for event in events[:-1]] == ["chunk"] * (len(events) - 1)
    assert events[-1]["type"] == "done"
    # The replacement may supersede the append before it was published; either way the latest snapshot arrives last.
    assert [(event["seq"], event["content"], event["replace"]) for event in events[:-1]] in (
        [(2, "replacement", True)],
        [(0, "a", False), (2, "replacement", True)],
    )
    assert all(event["turn_id"] == turn.message_id for event in events)
    assert events[-1]["message_id"] == str(assistant_id) != turn.message_id
    assert events[-1]["content"] == "answer"
    assert "first_delta" in timeline.marks


@pytest.mark.parametrize("origin", ["slack", "whatsapp", "agent", "missing", "burst", "shared_im"])
async def test_im_and_nonweb_turns_use_final_only_exec(monkeypatch, tmp_path, origin):
    turn = _Turn(monkeypatch, tmp_path)
    shared = origin in {"burst", "shared_im"}
    turn.message.conversation_id = str(shared_session_id(turn.agent.id) if shared else uuid4())
    turn.message.routing_reason = "selected_model"
    reads = []

    async def get_session(_id):
        return (
            None
            if origin == "missing"
            else ChatSessionRecord(uuid4(), turn.agent.id, turn.agent.creator_id, source_channel=origin)
        )

    async def batch(message_id):
        reads.append(message_id)
        return SimpleNamespace(gateway_id=message_id) if origin == "burst" else None

    async def has_im(_id):
        return origin == "shared_im"

    monkeypatch.setattr(streaming.chat_session_dao, "get", get_session)
    monkeypatch.setattr(streaming.personal_burst_dispatch_dao, "get", batch)
    monkeypatch.setattr(streaming.gateway_report_delivery_dao, "has_im_origin", has_im)
    outcome = await turn.run()
    assert isinstance(outcome, rpc.TerminalOutcome)
    assert turn.executed == 1
    assert reads == ([turn.message.id] if shared else [])


@pytest.mark.parametrize("read", ["session", "batch", "intent"])
async def test_origin_read_failure_prevents_dispatch(monkeypatch, tmp_path, read):
    turn = _Turn(monkeypatch, tmp_path)
    shared = read != "session"
    turn.message.conversation_id = str(shared_session_id(turn.agent.id) if shared else uuid4())

    async def failed(_id):
        raise RuntimeError("classification read failed")

    async def absent(_id):
        return None

    monkeypatch.setattr(streaming.chat_session_dao, "get", failed)
    monkeypatch.setattr(streaming.personal_burst_dispatch_dao, "get", absent if read == "intent" else failed)
    monkeypatch.setattr(streaming.gateway_report_delivery_dao, "has_im_origin", failed)
    with pytest.raises(RuntimeError, match="classification read failed"):
        await turn.run()
    assert turn.executed == 0
    assert not list(tmp_path.glob(".maraclaw-rpc-*.json"))


async def test_overflow_bounds_scheduled_callbacks_and_drops_without_blocking():
    message = GatewayMessageRecord(uuid4(), uuid4(), "hello")
    relay = streaming.DeltaRelay(message, None)

    def flood():
        for _ in range(streaming.QUEUE_SIZE + 100):
            relay.offer("{}")

    await asyncio.wait_for(asyncio.to_thread(flood), 5)
    assert relay.dropped == 100
    assert relay.queue.qsize() == streaming.QUEUE_SIZE
    relay.finish()
    await asyncio.wait_for(relay.publish(), 5)


async def test_exec_retains_strict_final_when_delta_queue_overflows(monkeypatch):
    message = GatewayMessageRecord(uuid4(), uuid4(), "hello")
    relay = streaming.DeltaRelay(message, None)

    @contextmanager
    def execute(*_args, **_kwargs):
        yield iter(
            [
                ("stdout", b"{}\n" * (streaming.QUEUE_SIZE + 100)),
                ("stdout", wire_terminal(str(message.id)).encode() + b"\n"),
            ]
        )

    monkeypatch.setattr(
        "app.services.agent_manager.agent_manager.docker_client",
        SimpleNamespace(container=SimpleNamespace(execute=execute)),
    )
    output = await asyncio.to_thread(relay.execute, "fake-container", "fake-payload", Event())
    assert isinstance(rpc._parse_outcome(output, str(message.id)), rpc.TerminalOutcome)
    assert relay.dropped == 101
    relay.finish()
    await relay.publish()


async def test_socket_failure_cannot_fail_live_notification(monkeypatch):
    async def disconnected(*_args, **_kwargs):
        raise RuntimeError("socket closed")

    monkeypatch.setattr(manager, "send_to_session", disconnected)
    message = GatewayMessageRecord(uuid4(), uuid4(), "hello", conversation_id=str(uuid4()))
    delta = streaming.GatewayDelta.model_validate(
        {"kind": "delta", "messageId": str(message.id), "seq": 0, "text": "hello", "replace": False}
    )
    await delivery.publish_chunk(message, delta)


async def test_installed_pywhales_stream_consumes_isolated_cli(monkeypatch):
    message = GatewayMessageRecord(uuid4(), uuid4(), "hello")
    output = "{}\n" + wire_terminal(str(message.id)) + "\n"
    client = DockerClient(
        client_call=[sys.executable, "-c", f"import sys; sys.stdout.write({output!r})"],
        inherit_environment=False,
    )
    monkeypatch.setattr("app.services.agent_manager.agent_manager.docker_client", client)
    relay = streaming.DeltaRelay(message, None)
    result = await asyncio.to_thread(relay.execute, "isolated-cli", "unused-payload", Event())
    assert isinstance(rpc._parse_outcome(result, str(message.id)), rpc.TerminalOutcome)
    relay.finish()
    await relay.publish()


async def test_streaming_cancellation_drains_worker_before_payload_cleanup(monkeypatch, tmp_path):
    turn = _Turn(monkeypatch, tmp_path)
    turn.message.conversation_id = str(uuid4())
    started = asyncio.Event()
    cancelled = asyncio.Event()
    release = Event()
    loop = asyncio.get_running_loop()

    async def web(_message):
        return True

    def execute(_self, _container, payload, cancel):
        loop.call_soon_threadsafe(started.set)
        assert cancel.wait(5)
        loop.call_soon_threadsafe(cancelled.set)
        assert release.wait(5)
        assert (tmp_path / payload).exists()
        return wire_terminal(turn.message_id)

    monkeypatch.setattr(rpc, "streams_to_web", web)
    monkeypatch.setattr(streaming.DeltaRelay, "execute", execute)
    task = asyncio.create_task(turn.run())
    try:
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        await asyncio.wait_for(cancelled.wait(), 5)
        task.cancel()
        assert not task.done()
        assert len(list(tmp_path.glob(".maraclaw-rpc-*.json"))) == 1
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert not list(tmp_path.glob(".maraclaw-rpc-*.json"))


async def test_configured_model_id_split_across_deltas_never_reaches_the_socket(monkeypatch, tmp_path):
    turn = _Turn(monkeypatch, tmp_path)
    turn.message.conversation_id = str(uuid4())
    turn.message.guest_model_ref = REF
    events = []
    delivered = Event()
    pieces = ["Running on openrouter/nv", "idia/nemotron-3.5-su", "per-vl:fr", "ee, ok."]

    async def get_session(_id):
        return ChatSessionRecord(uuid4(), turn.agent.id, turn.agent.creator_id)

    async def send(_agent_id, _conversation_id, event, *, user_id, send_timeout=None):
        assert send_timeout == delivery.CHUNK_SEND_TIMEOUT_SECONDS
        events.append(event)
        if event["content"].endswith("ok."):
            delivered.set()

    @contextmanager
    def execute(_container, _argv, *, stream, timeout):
        lines = [
            json.dumps({"kind": "delta", "messageId": turn.message_id, "seq": seq, "text": piece})
            for seq, piece in enumerate(pieces)
        ]

        def chunks():
            yield "stdout", ("\n".join(lines) + "\n").encode()
            assert delivered.wait(5), "every delta must reach the socket before the terminal outcome"
            yield "stdout", wire_terminal(turn.message_id).encode()

        yield chunks()

    monkeypatch.setattr(streaming.chat_session_dao, "get", get_session)
    monkeypatch.setattr(manager, "send_to_session", send)
    monkeypatch.setattr(rpc, "_policy", streaming.EXEC_POLICY)
    monkeypatch.setattr(
        "app.services.agent_manager.agent_manager.docker_client",
        SimpleNamespace(
            container=SimpleNamespace(
                execute=execute, inspect=lambda _: SimpleNamespace(state=SimpleNamespace(running=True))
            )
        ),
    )

    assert isinstance(await asyncio.wait_for(turn.run(), 5), rpc.TerminalOutcome)

    contents = [event["content"] for event in events if event["type"] == "chunk"]
    assert "".join(contents) == f"Running on {BRAND}, ok."
    assert not [c for c in contents if any(word in c.lower() for word in ("openrouter", "nvidia", "nemotron", "super"))]


async def test_dropped_delta_withholds_appends_until_a_snapshot_resynchronizes(monkeypatch):
    message = GatewayMessageRecord(uuid4(), uuid4(), "hello", conversation_id=str(uuid4()), guest_model_ref=REF)
    events = []

    async def send(_agent_id, _conversation_id, event, *, user_id, send_timeout=None):
        assert send_timeout == delivery.CHUNK_SEND_TIMEOUT_SECONDS
        events.append((event["seq"], event["content"], event["replace"]))

    monkeypatch.setattr(manager, "send_to_session", send)
    relay = streaming.DeltaRelay(message, None)
    deltas = [(0, "safe ", False), (2, "uper-vl:free tail", False), (3, f"snapshot {REF} end ", True), (4, "!", False)]

    for seq, text, replace in deltas:
        relay.offer(
            json.dumps({"kind": "delta", "messageId": str(message.id), "seq": seq, "text": text, "replace": replace})
        )
    await asyncio.sleep(0)  # let the loop run the scheduled queue puts before completion is signalled
    relay.finish()
    await asyncio.wait_for(relay.publish(), 5)

    # All four were queued before the first publication, so the snapshot supersedes the append and the "!" joins it.
    assert events == [(4, f"snapshot {BRAND} end !", True)]


async def test_chunk_timing_counts_failed_publications_and_keeps_the_slowest(monkeypatch):
    message = GatewayMessageRecord(uuid4(), uuid4(), "hello", conversation_id=str(uuid4()))
    timeline = TurnTimeline(message.id, message.agent_id, None)
    relay = streaming.DeltaRelay(message, timeline)
    probe = IngestProbe(monkeypatch, relay)
    now = 100.0
    in_flight, release = asyncio.Event(), asyncio.Event()
    sends = 0

    async def send(_agent_id, _conversation_id, _event, *, user_id, send_timeout=None):
        nonlocal now, sends
        sends += 1
        if sends == 1:
            now += 0.25
            in_flight.set()
            await release.wait()
            return
        now += 0.75
        raise RuntimeError("socket closed")

    monkeypatch.setattr(manager, "send_to_session", send)
    monkeypatch.setattr(streaming, "monotonic", lambda: now)
    consumer = asyncio.create_task(relay.publish())
    try:
        relay.offer(delta_line(message, 0, "first "))
        await asyncio.wait_for(in_flight.wait(), 5)
        relay.offer(delta_line(message, 1, "second"))
        await probe.ingested(1)

        release.set()
        await asyncio.wait_for(relay.drain(consumer), 5)
        await consumer
    finally:
        consumer.cancel()
        with suppress(asyncio.CancelledError):
            await consumer

    assert sends == 2
    assert (timeline.chunks_published, timeline.slowest_chunk_ms) == (2, 750)


async def test_chunk_timing_counts_a_publication_that_raised(monkeypatch):
    message = GatewayMessageRecord(uuid4(), uuid4(), "hello", conversation_id=str(uuid4()))
    timeline = TurnTimeline(message.id, message.agent_id, None)
    relay = streaming.DeltaRelay(message, timeline)

    async def broken(_message, _delta):
        raise RuntimeError("publication failed")

    monkeypatch.setattr(delivery, "publish_chunk", broken)
    relay.offer(delta_line(message, 0, "x"))
    await asyncio.sleep(0)  # let the loop run the scheduled queue put before completion is signalled
    relay.finish()

    with pytest.raises(ExceptionGroup):
        await asyncio.wait_for(relay.publish(), 5)

    assert timeline.chunks_published == 1
