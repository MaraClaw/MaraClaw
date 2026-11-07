import asyncio
import threading
import uuid
from types import SimpleNamespace

import lark_oapi.ws.client as lark_ws_client
import pytest

from app.runtime import tasks
from app.runtime.tasks import EventTasks, OwnedTasks
from app.services import feishu_ws
from app.services.dingtalk_stream import DingTalkStreamManager
from app.services.feishu_ws import FeishuWSManager


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_owned_completed_tasks_are_released_and_failures_observed(
    monkeypatch: pytest.MonkeyPatch, fails: bool
) -> None:
    # Given
    owner = OwnedTasks()
    completed = asyncio.Event()
    error = RuntimeError("message processing failed")
    reported: list[tuple[str, BaseException]] = []

    def record(_message: str, name: str, failure: BaseException) -> None:
        reported.append((name, failure))

    async def message() -> None:
        if fails:
            raise error

    monkeypatch.setattr(tasks.logger, "error", record)
    owner.start("message", message())
    task = next(iter(owner.tasks))
    task.add_done_callback(lambda _: completed.set())
    # When
    await completed.wait()
    # Then
    assert not owner.tasks
    assert reported == ([("message", error)] if fails else [])


@pytest.mark.asyncio
async def test_sdk_event_children_are_cancelled_and_awaited() -> None:
    # Given
    owner = EventTasks()
    started = asyncio.Event()
    finished = asyncio.Event()

    @owner.wrap
    async def callback() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    task = asyncio.create_task(callback())
    await started.wait()
    # When
    await owner.stop()
    # Then
    assert finished.is_set()
    assert task.cancelled()
    assert not owner.tasks


@pytest.mark.asyncio
async def test_dingtalk_thread_join_does_not_block_main_loop() -> None:
    # Given
    manager = DingTalkStreamManager()
    aid = uuid.uuid4()
    release = threading.Event()
    thread = threading.Thread(target=release.wait)
    thread.start()
    manager._threads[aid] = thread
    manager._stop_events[aid] = threading.Event()

    async def release_from_main_loop() -> None:
        await asyncio.sleep(0)
        release.set()

    heartbeat = asyncio.create_task(release_from_main_loop())
    # When
    try:
        await manager.stop_all()
    finally:
        release.set()
        await asyncio.to_thread(thread.join, 1)
        await heartbeat
    # Then
    assert not thread.is_alive()
    assert not manager._threads


class FakeLarkClient:
    def __init__(self, failures: int) -> None:
        self.calls: list[str] = []
        self.failures = failures
        self.on_disconnect: list[object] = []
        self.ping_started = asyncio.Event()

    async def _connect(self) -> None:
        self.calls.append("connect")
        if self.failures > 0:
            self.failures -= 1
            raise ConnectionError("handshake refused")

    async def _disconnect(self) -> None:
        self.calls.append("disconnect")
        for callback in self.on_disconnect:
            if callable(callback):
                callback()

    async def _ping_loop(self) -> None:
        self.ping_started.set()
        await asyncio.Event().wait()


class FakeEventBuilder:
    def register_p2_customized_event(self, _name: str, _handler: object) -> FakeEventBuilder:
        return self

    def build(self) -> object:
        return object()


def install_fake_lark(monkeypatch: pytest.MonkeyPatch, client: FakeLarkClient) -> None:
    monkeypatch.setattr(lark_ws_client, "loop", lark_ws_client.loop)
    monkeypatch.setattr(feishu_ws, "_HAS_LARK", True)
    monkeypatch.setattr(feishu_ws, "_PROXY_PATCH_AVAILABLE", False)
    monkeypatch.setattr(
        feishu_ws,
        "_lark_mod",
        SimpleNamespace(
            LogLevel=SimpleNamespace(INFO=1),
            EventDispatcherHandler=SimpleNamespace(builder=lambda *_: FakeEventBuilder()),
        ),
    )
    monkeypatch.setattr(feishu_ws, "_lark_ws_mod", SimpleNamespace(Client=lambda *_args, **_kwargs: client))
    monkeypatch.setattr(feishu_ws, "_initial_connect_delay", lambda attempt: 10.0 + attempt)


@pytest.mark.asyncio
async def test_feishu_initial_connect_retries_then_leaves_reconnection_to_the_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an SDK client whose first two handshakes fail, and sleeps that only yield to the loop.
    client = FakeLarkClient(failures=2)
    install_fake_lark(monkeypatch, client)
    real_sleep = asyncio.sleep
    delays: list[float] = []

    async def yielding_sleep(delay: float) -> None:
        delays.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(feishu_ws.asyncio, "sleep", yielding_sleep)
    manager, agent_id = FeishuWSManager(), uuid.uuid4()
    # When the manager starts the client and the health watch spins with the SDK connection missing.
    await manager.start_client(agent_id, "app", "secret")
    await asyncio.wait_for(client.ping_started.wait(), 5)
    for _ in range(50):
        await real_sleep(0)
    # Then each failure is followed by a disconnect and a backoff, and a connected client is never connected again.
    assert client.calls == ["connect", "disconnect", "connect", "disconnect", "connect"]
    assert delays[:2] == [10.0, 11.0]
    await manager.stop_client(agent_id)


@pytest.mark.asyncio
async def test_feishu_initial_connect_retry_stops_when_the_manager_is_closing(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a client that never connects and a manager that starts closing during the first disconnect.
    client = FakeLarkClient(failures=1000)
    install_fake_lark(monkeypatch, client)
    manager, agent_id = FeishuWSManager(), uuid.uuid4()
    client.on_disconnect.append(lambda: setattr(manager, "_closing", True))
    # When the client starts.
    await manager.start_client(agent_id, "app", "secret")
    await asyncio.wait_for(manager._tasks[agent_id], 5)
    # Then the retry loop ends without another attempt.
    assert client.calls == ["connect", "disconnect"]


@pytest.mark.asyncio
async def test_feishu_initial_connect_retry_wait_is_cancelled_by_stop_client(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a failed first connect and a backoff wait that never ends on its own.
    client = FakeLarkClient(failures=1000)
    install_fake_lark(monkeypatch, client)
    waiting = asyncio.Event()

    async def blocked_sleep(_delay: float) -> None:
        waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(feishu_ws.asyncio, "sleep", blocked_sleep)
    manager, agent_id = FeishuWSManager(), uuid.uuid4()
    await manager.start_client(agent_id, "app", "secret")
    task = manager._tasks[agent_id]
    await asyncio.wait_for(waiting.wait(), 5)
    # When the channel client is stopped mid-wait.
    await manager.stop_client(agent_id)
    # Then the wait is cancelled and the client is not connected again.
    assert task.done()
    assert client.calls.count("connect") == 1


@pytest.mark.parametrize(
    ("attempt", "unit", "expected"),
    [(0, 0.0, 1.0), (0, 1.0, 2.0), (3, 0.0, 8.0), (30, 0.0, 30.0), (30, 1.0, 60.0)],
)
def test_feishu_initial_connect_delay_is_capped_and_jittered(
    monkeypatch: pytest.MonkeyPatch, attempt: int, unit: float, expected: float
) -> None:
    # Given a jitter source pinned to one end of its range.
    monkeypatch.setattr(feishu_ws, "_RANDOM", SimpleNamespace(random=lambda: unit))
    # When the delay is computed, then it is the capped backoff scaled to 50-100%.
    assert feishu_ws._initial_connect_delay(attempt) == pytest.approx(expected)
