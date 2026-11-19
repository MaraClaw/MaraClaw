import asyncio
import json
import threading
from uuid import uuid4

import dingtalk_stream
import httpx
import pytest
from websockets.asyncio.server import serve

from app.api import dingtalk
from app.services import dingtalk_stream as dingtalk_manager
from app.services.channels import dingtalk_stream_transport
from app.services.dingtalk_stream import DingTalkStreamManager
from stream_source_fixtures import connection_config


@pytest.mark.parametrize("phase", ["open", "receive", "callback"])
async def test_real_sdk_transport_stops_joins_and_restarts_during_connection_or_acceptance(monkeypatch, phase):
    manager, agent_id = DingTalkStreamManager(), uuid4()
    connection_config(monkeypatch, agent_id, "dingtalk")
    loop = asyncio.get_running_loop()
    opening, connected, accepting, acknowledged = (asyncio.Event() for _ in range(4))
    control_ack = asyncio.Event()
    release = asyncio.Event()
    requests, sockets, acknowledgements, committed, threads = [], [], [], [], []
    entered, controls = [], []
    fresh = False

    async def accept(**kwargs):
        entered.append(kwargs)
        if len(entered) == 8:
            accepting.set()
        await release.wait()
        committed.append(kwargs)

    async def provider(socket):
        sockets.append(socket)
        connected.set()
        if phase == "callback" or fresh:
            for index in range(1 if fresh else 8):
                await socket.send(
                    json.dumps(
                        {
                            "type": "CALLBACK",
                            "headers": {"topic": dingtalk_stream.ChatbotMessage.TOPIC, "messageId": str(index)},
                            "data": json.dumps({"msgtype": "text", "msgId": str(index), "text": {"content": "hello"}}),
                        }
                    )
                )
            await socket.send(
                json.dumps({"type": "SYSTEM", "headers": {"topic": "ping", "messageId": "control"}, "data": "{}"})
            )
            async for response in socket:
                ack = json.loads(response)
                if ack["headers"]["messageId"] == "control":
                    controls.append(ack["code"])
                    control_ack.set()
                else:
                    acknowledgements.append(ack["code"])
                    acknowledged.set()
        else:
            await socket.wait_closed()

    original_http = httpx.AsyncClient
    monkeypatch.setattr(dingtalk, "process_dingtalk_message", accept)
    async with serve(provider, "127.0.0.1", 0) as server:
        port = next(iter(server.sockets)).getsockname()[1]

        async def open_connection(request):
            requests.append(json.loads(request.content))
            loop.call_soon_threadsafe(opening.set)
            if phase == "open" and not fresh:
                await asyncio.Future()
            return httpx.Response(200, json={"endpoint": f"ws://127.0.0.1:{port}", "ticket": "ticket value"})

        def http_client(**kwargs):
            return original_http(**kwargs, transport=httpx.MockTransport(open_connection))

        monkeypatch.setattr(dingtalk_stream_transport.httpx, "AsyncClient", http_client)
        try:
            await manager.start_client(agent_id, "app", "secret")
            threads.append(manager._threads[agent_id])
            await asyncio.wait_for(opening.wait(), 2)
            if phase != "open":
                await asyncio.wait_for(connected.wait(), 2)
            if phase == "callback":
                await asyncio.wait_for(accepting.wait(), 2)
                await asyncio.wait_for(control_ack.wait(), 2)
                assert controls == [dingtalk_stream.AckMessage.STATUS_OK]
            assert acknowledgements == committed == []
            await asyncio.wait_for(manager.stop_client(agent_id), 3)
            assert not threads[-1].is_alive()
            assert all(code != dingtalk_stream.AckMessage.STATUS_OK for code in acknowledgements)
            assert requests[0]["subscriptions"] == [{"type": "CALLBACK", "topic": dingtalk_stream.ChatbotMessage.TOPIC}]
            assert requests[0]["clientId"] == "app"
            assert requests[0]["clientSecret"] == "secret"
            fresh = True
            acknowledged.clear()
            release.set()
            await manager.start_client(agent_id, "app", "secret")
            threads.append(manager._threads[agent_id])
            await asyncio.wait_for(acknowledged.wait(), 2)
            assert acknowledgements[-1] == dingtalk_stream.AckMessage.STATUS_OK
            assert len(committed) == 1
        finally:
            release.set()
            await manager.stop_all()
        assert all(not thread.is_alive() for thread in threads)
        assert all(socket.close_code is not None for socket in sockets)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def pin_reconnect_timing(monkeypatch, value: float) -> FakeClock:
    clock = FakeClock()
    monkeypatch.setattr(dingtalk_manager, "_random_source", lambda: value)
    monkeypatch.setattr(dingtalk_manager, "_clock", clock)
    return clock


def record_waits(monkeypatch, stop: threading.Event) -> list[float]:
    delays: list[float] = []

    def backoff(timeout):
        delays.append(timeout)
        return stop.is_set()

    monkeypatch.setattr(stop, "wait", backoff)
    return delays


async def test_connection_recovers_after_more_than_six_transient_failures(monkeypatch):
    pin_reconnect_timing(monkeypatch, 0.5)
    manager, stop = DingTalkStreamManager(), threading.Event()
    attempts = []

    def run(transport, client):
        attempts.append(client)
        if len(attempts) <= 8:
            raise ConnectionError("provider unavailable")
        stop.set()

    delays = record_waits(monkeypatch, stop)
    monkeypatch.setattr(dingtalk_stream_transport.DingTalkTransport, "run", run)
    await asyncio.to_thread(manager._run_client_thread, uuid4(), "app", "secret", stop)
    assert len(attempts) == 9
    assert delays == [3, 6, 12, 24, 45, 45, 45, 45]


async def test_client_that_ends_immediately_escalates_instead_of_restarting_at_two_seconds(monkeypatch):
    # Given a client that connects and drops at once, every time.
    pin_reconnect_timing(monkeypatch, 0.0)
    manager, stop = DingTalkStreamManager(), threading.Event()
    attempts = []

    def run(transport, client):
        attempts.append(client)
        if len(attempts) == 7:
            stop.set()

    delays = record_waits(monkeypatch, stop)
    monkeypatch.setattr(dingtalk_stream_transport.DingTalkTransport, "run", run)
    # When the thread keeps reconnecting.
    await asyncio.to_thread(manager._run_client_thread, uuid4(), "app", "secret", stop)
    # Then the delays keep growing to the cap instead of resetting to the first one.
    assert delays == [2, 4, 8, 16, 30, 30]


@pytest.mark.parametrize("starts_over", [False, True])
async def test_client_that_stayed_up_past_the_healthy_threshold_starts_over(monkeypatch, starts_over):
    # Given two fast failures followed by a run that lasts just under, or exactly, the healthy threshold.
    clock = pin_reconnect_timing(monkeypatch, 0.0)
    healthy = dingtalk_manager.DINGTALK_HEALTHY_RUN_SECONDS
    manager, stop = DingTalkStreamManager(), threading.Event()
    attempts = []

    def run(transport, client):
        attempts.append(client)
        if len(attempts) == 3:
            clock.now += healthy if starts_over else healthy - 0.1
        if len(attempts) == 4:
            stop.set()

    delays = record_waits(monkeypatch, stop)
    monkeypatch.setattr(dingtalk_stream_transport.DingTalkTransport, "run", run)
    await asyncio.to_thread(manager._run_client_thread, uuid4(), "app", "secret", stop)
    # Then only the healthy run makes the next delay the first one again.
    assert delays == [2, 4, 2 if starts_over else 8]


async def test_client_build_errors_after_a_long_run_still_count_as_fast_failures(monkeypatch):
    # Given one long run, after which the client can no longer be built.
    clock = pin_reconnect_timing(monkeypatch, 0.0)
    manager, stop = DingTalkStreamManager(), threading.Event()
    builds = []
    build = dingtalk_stream.DingTalkStreamClient

    def broken_build(*args, **kwargs):
        builds.append(kwargs)
        if len(builds) == 1:
            return build(*args, **kwargs)
        if len(builds) == 4:
            stop.set()
        raise RuntimeError("cannot build client")

    def run(transport, client):
        clock.now += 120

    delays = record_waits(monkeypatch, stop)
    monkeypatch.setattr(dingtalk_stream, "DingTalkStreamClient", broken_build)
    monkeypatch.setattr(dingtalk_stream_transport.DingTalkTransport, "run", run)
    await asyncio.to_thread(manager._run_client_thread, uuid4(), "app", "secret", stop)
    # Then the build errors escalate from their own start instead of inheriting the old run's health.
    assert delays == [2, 4, 8, 16]


def test_stop_while_waiting_to_reconnect_ends_the_thread_without_waiting_out_the_delay(monkeypatch):
    # Given a client that fails at once and a retry delay of an hour.
    monkeypatch.setattr(dingtalk_manager, "DINGTALK_RECONNECT_BASE_SECONDS", 3600.0)
    monkeypatch.setattr(dingtalk_manager, "DINGTALK_RECONNECT_CAP_SECONDS", 3600.0)
    monkeypatch.setattr(dingtalk_stream_transport.DingTalkTransport, "run", lambda transport, client: None)
    manager, stop, waiting = DingTalkStreamManager(), threading.Event(), threading.Event()
    wait = stop.wait

    def observed_wait(timeout=None):
        waiting.set()
        return wait(timeout)

    monkeypatch.setattr(stop, "wait", observed_wait)
    thread = threading.Thread(target=manager._run_client_thread, args=(uuid4(), "app", "secret", stop), daemon=True)
    thread.start()
    try:
        # When stop is requested once the thread is parked in its retry wait.
        assert waiting.wait(5)
        stop.set()
        thread.join(5)
        # Then the wait ends as soon as the client is stopped.
        assert not thread.is_alive()
    finally:
        stop.set()
        thread.join(5)


async def test_overlapping_stop_and_restart_wait_for_old_callback_cleanup_before_transport_teardown(monkeypatch):
    manager, agent_id = DingTalkStreamManager(), uuid4()
    connection_config(monkeypatch, agent_id, "dingtalk")
    loop = asyncio.get_running_loop()
    started, cleaning, release, fresh = (asyncio.Event() for _ in range(4))
    clients, stop_calls, operations = [], [], []

    async def accept(**kwargs):
        started.set()
        try:
            await asyncio.Future()
        finally:
            cleaning.set()
            await release.wait()

    async def send(payload):
        return None

    def run(transport, client):
        clients.append(client)
        client.websocket = type("Socket", (), {"send": staticmethod(send)})()

        async def receive():
            if len(clients) == 1:
                await client.route_message(
                    {
                        "type": "CALLBACK",
                        "headers": {"topic": dingtalk_stream.ChatbotMessage.TOPIC, "messageId": "delivery"},
                        "data": json.dumps({"msgtype": "text", "msgId": "native", "text": {"content": "hello"}}),
                    }
                )
            else:
                loop.call_soon_threadsafe(fresh.set)
                await asyncio.to_thread(transport._stop_event.wait)

        asyncio.run(receive())

    stop_transport = dingtalk_stream_transport.DingTalkTransport.stop

    def record_stop(transport):
        stop_calls.append(transport)
        stop_transport(transport)

    monkeypatch.setattr(dingtalk, "process_dingtalk_message", accept)
    monkeypatch.setattr(dingtalk_stream_transport.DingTalkTransport, "run", run)
    monkeypatch.setattr(dingtalk_stream_transport.DingTalkTransport, "stop", record_stop)
    try:
        await manager.start_client(agent_id, "app", "secret")
        old_thread = manager._threads[agent_id]
        await asyncio.wait_for(started.wait(), 1)
        operations.append(asyncio.create_task(manager.stop_client(agent_id)))
        await asyncio.wait_for(cleaning.wait(), 1)
        operations.append(asyncio.create_task(manager.stop_client(agent_id)))
        operations.append(asyncio.create_task(manager.start_client(agent_id, "app", "secret")))
        await asyncio.sleep(0)
        assert stop_calls == []
        assert len(clients) == 1
        assert all(not operation.done() for operation in operations)
        release.set()
        await asyncio.wait_for(asyncio.gather(*operations), 2)
        await asyncio.wait_for(fresh.wait(), 1)
        fresh_thread = manager._threads[agent_id]
        assert fresh_thread is not old_thread
        assert not old_thread.is_alive()
        assert fresh_thread.is_alive()
        assert len(clients) == 2
    finally:
        release.set()
        await asyncio.gather(*operations, return_exceptions=True)
        await manager.stop_all()
    assert not fresh_thread.is_alive()


async def test_restart_joins_old_worker_before_replacing_its_stop_event(monkeypatch):
    manager, agent_id = DingTalkStreamManager(), uuid4()
    connection_config(monkeypatch, agent_id, "dingtalk")
    loop = asyncio.get_running_loop()
    returned, joining, fresh = (asyncio.Event() for _ in range(3))
    finish = threading.Event()
    threads, transports, operations = [], [], []
    worker = manager._run_client_thread

    def hold_exit(*args):
        threads.append(threading.current_thread())
        worker(*args)
        if len(threads) == 1:
            loop.call_soon_threadsafe(returned.set)
            finish.wait()

    def run(transport, client):
        transports.append(transport)
        if len(transports) == 1:
            transport._stop_event.set()
        else:
            loop.call_soon_threadsafe(fresh.set)
            transport._stop_event.wait()

    monkeypatch.setattr(manager, "_run_client_thread", hold_exit)
    monkeypatch.setattr(dingtalk_stream_transport.DingTalkTransport, "run", run)
    try:
        await manager.start_client(agent_id, "app", "secret")
        await asyncio.wait_for(returned.wait(), 1)
        old_thread = threads[0]
        join = old_thread.join

        def record_join(timeout=None):
            loop.call_soon_threadsafe(joining.set)
            join(timeout)

        monkeypatch.setattr(old_thread, "join", record_join)
        restart = asyncio.create_task(manager.start_client(agent_id, "app", "secret"))
        operations.append(restart)
        await asyncio.wait_for(joining.wait(), 1)
        assert not restart.done()
        assert len(transports) == 1
        finish.set()
        await asyncio.wait_for(restart, 1)
        await asyncio.wait_for(fresh.wait(), 1)
        assert not old_thread.is_alive()
        assert manager._threads[agent_id] is threads[1]
        assert manager._stop_events[agent_id] is transports[1]._stop_event
    finally:
        finish.set()
        await asyncio.gather(*operations, return_exceptions=True)
        await manager.stop_all()
        for thread in threads:
            await asyncio.to_thread(thread.join, 1)
    assert all(not thread.is_alive() for thread in threads)
