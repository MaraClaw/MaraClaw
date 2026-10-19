from __future__ import annotations

import asyncio
from pathlib import Path
from threading import BoundedSemaphore, Event
from typing import override

import pytest

from app.services.document_parser import DocumentParseError, runtime


class ControlledProcess:
    pid: int | None = None
    exitcode: int | None = None

    def __init__(self, *, start_error: bool = False, join_error: bool = False) -> None:
        self.started = Event()
        self.cleaning = Event()
        self.allow_reap = Event()
        self.reaped = Event()
        self.start_error = start_error
        self.join_error = join_error
        self.alive = True
        self.root: Path | None = None

    def start(self) -> None:
        self.started.set()
        if self.start_error:
            self.pid = 123456789
            raise OSError("start failed after launch")

    def join(self, timeout: float | None = None) -> None:
        if self.join_error:
            self.join_error = False
            raise OSError("join failed")
        if self.cleaning.is_set():
            if self.allow_reap.wait(timeout):
                self.alive = False
                self.reaped.set()
        else:
            self.cleaning.wait(min(timeout or 0.01, 0.01))

    def is_alive(self) -> bool:
        return self.alive

    def terminate(self) -> None:
        self.cleaning.set()

    def kill(self) -> None:
        self.cleaning.set()


def bind_process(monkeypatch: pytest.MonkeyPatch, process: ControlledProcess) -> BoundedSemaphore:
    permits = BoundedSemaphore(1)

    class Context:
        def Process(self, **kwargs: object) -> ControlledProcess:  # noqa: N802
            args = kwargs["args"]
            assert isinstance(args, tuple)
            process.root = Path(str(args[0])).parent
            return process

    monkeypatch.setattr(runtime, "_CONVERT_PERMITS", permits)
    monkeypatch.setattr(runtime.mp, "get_context", lambda _: Context())
    return permits


async def wait_event(event: Event) -> None:
    assert await asyncio.to_thread(event.wait, 5), "lifecycle event was not reached"


@pytest.mark.parametrize("sheets", [False, True])
async def test_cancel_when_waiting_for_admission_never_starts_late_worker(
    monkeypatch: pytest.MonkeyPatch, sheets: bool
) -> None:
    # Given
    process = ControlledProcess()
    process.allow_reap.set()
    permits = bind_process(monkeypatch, process)
    assert permits.acquire(blocking=False)
    convert = runtime.extract_xlsx_sheets_isolated_async if sheets else runtime.convert_document_isolated_async
    task = asyncio.create_task(convert(b"document", "report.xlsx", slot_wait_seconds=0.2, timeout_seconds=0.1))
    await asyncio.sleep(0)
    # When
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    permits.release()
    # Then: drain the old implementation's abandoned thread before checking.
    await asyncio.to_thread(process.started.wait, 0.3)
    assert not process.started.is_set()
    assert permits.acquire(timeout=1)
    permits.release()


async def test_repeated_cancel_waits_for_reap_without_blocking_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    process = ControlledProcess()
    permits = bind_process(monkeypatch, process)
    task = asyncio.create_task(runtime.convert_document_isolated_async(b"document", "report.docx", timeout_seconds=0.2))
    await wait_event(process.started)
    # When
    task.cancel()
    await wait_event(process.cleaning)
    task.cancel()
    await asyncio.sleep(0)
    try:
        # Then
        assert not task.done(), "cancellation escaped before cleanup completed"
        assert not permits.acquire(blocking=False)
        assert process.root is not None
        assert process.root.exists()
    finally:
        process.allow_reap.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await wait_event(process.reaped)
    assert permits.acquire(timeout=1)
    permits.release()
    assert process.root is not None
    assert not process.root.exists()


@pytest.mark.parametrize("failure", ["start", "join"])
async def test_lifecycle_failure_cleans_worker_before_returning(monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    # Given
    process = ControlledProcess(start_error=failure == "start", join_error=failure == "join")
    process.allow_reap.set()
    permits = bind_process(monkeypatch, process)
    # When
    with pytest.raises(DocumentParseError):
        await runtime.convert_document_isolated_async(b"document", "report.docx")
    # Then
    assert process.reaped.is_set()
    assert permits.acquire(blocking=False)
    permits.release()
    assert process.root is not None
    assert not process.root.exists()


async def test_unreaped_worker_quarantines_capacity_and_temporary_files(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    process = ControlledProcess()
    permits = bind_process(monkeypatch, process)
    monkeypatch.setattr(runtime, "kill_process", lambda _process: False)
    # When
    with pytest.raises(runtime.DocumentParseTimeoutError) as exc:
        await runtime.convert_document_isolated_async(b"document", "report.docx", timeout_seconds=0)
    # Then
    try:
        assert not exc.value.child_reaped
        assert not permits.acquire(blocking=False)
        assert process.root is not None
        assert process.root.exists()
        assert runtime._QUARANTINED_JOBS[-1][0] is process
    finally:
        retained_process, temporary = runtime._QUARANTINED_JOBS.pop()
        assert retained_process is process
        process.alive = False
        temporary.cleanup()
        permits.release()


async def test_cancel_preserves_cancellation_when_cleanup_quarantines_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    process = ControlledProcess()
    permits = bind_process(monkeypatch, process)

    def failed_cleanup(_process: object) -> bool:
        process.cleaning.set()
        assert process.allow_reap.wait(5)
        return False

    monkeypatch.setattr(runtime, "kill_process", failed_cleanup)
    task = asyncio.create_task(runtime.convert_document_isolated_async(b"document", "report.docx"))
    await wait_event(process.started)
    # When
    task.cancel()
    await wait_event(process.cleaning)
    task.cancel()
    await asyncio.sleep(0)
    try:
        # Then
        assert not task.done()
        assert not permits.acquire(blocking=False)
    finally:
        process.allow_reap.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        retained_process, temporary = runtime._QUARANTINED_JOBS.pop()
        assert retained_process is process
        process.alive = False
        temporary.cleanup()
        permits.release()


async def test_start_failure_before_launch_restores_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    class UnstartedProcess(ControlledProcess):
        @override
        def start(self) -> None:
            raise OSError("could not launch")

    process = UnstartedProcess()
    permits = bind_process(monkeypatch, process)
    # When
    with pytest.raises(DocumentParseError):
        await runtime.convert_document_isolated_async(b"document", "report.docx")
    # Then
    assert not process.cleaning.is_set()
    assert permits.acquire(blocking=False)
    permits.release()
    assert process.root is not None
    assert not process.root.exists()


async def test_temporary_directory_failure_restores_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    process = ControlledProcess()
    permits = bind_process(monkeypatch, process)

    def fail_directory(*_args: object, **_kwargs: object) -> None:
        raise OSError("temporary disk unavailable")

    monkeypatch.setattr(runtime.tempfile, "TemporaryDirectory", fail_directory)
    # When
    with pytest.raises(DocumentParseError):
        await runtime.convert_document_isolated_async(b"document", "report.docx")
    # Then
    assert not process.started.is_set()
    assert permits.acquire(blocking=False)
    permits.release()
