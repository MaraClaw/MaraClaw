import asyncio
import threading
import uuid

import pytest

from app.runtime import tasks
from app.runtime.tasks import EventTasks, OwnedTasks
from app.services.dingtalk_stream import DingTalkStreamManager


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
