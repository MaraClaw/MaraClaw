import asyncio

import pytest

from app.api import wecom as wecom_api


class RecordingLogger:
    def __init__(self) -> None:
        self.debug_messages: list[str] = []
        self.error_messages: list[str] = []

    def debug(self, message: str) -> None:
        self.debug_messages.append(message)

    def opt(self, **_kwargs: object) -> RecordingLogger:
        return self

    def error(self, message: str) -> None:
        self.error_messages.append(message)


@pytest.mark.asyncio
async def test_wecom_background_task_logs_failure_and_releases_task(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: a scheduled WeCom task that fails
    logger = RecordingLogger()
    monkeypatch.setattr(wecom_api, "logger", logger)

    async def fail() -> None:
        raise RuntimeError("boom")

    # When: the task completes with an exception
    wecom_api._schedule_background(fail())
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # Then: its failure is observed and logged
    assert logger.error_messages == ["WeCom background task failed"]
    assert not wecom_api._background_tasks


@pytest.mark.asyncio
async def test_wecom_background_task_logs_cancellation_and_releases_task(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: a scheduled WeCom task blocked until cancellation
    logger = RecordingLogger()
    monkeypatch.setattr(wecom_api, "logger", logger)
    release = asyncio.Event()

    async def wait_for_release() -> None:
        await release.wait()

    wecom_api._schedule_background(wait_for_release())
    task = next(iter(wecom_api._background_tasks))
    await asyncio.sleep(0)

    # When: the task is cancelled
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # Then: cancellation is observed without being treated as a failure
    assert logger.debug_messages == ["WeCom background task cancelled"]
    assert not logger.error_messages
    assert not wecom_api._background_tasks
