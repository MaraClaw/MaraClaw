from __future__ import annotations

import asyncio
import contextlib
import json
import multiprocessing as mp
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from app.services.document_parser import runtime


def descendant_worker(src: str, _filename: str, _out: str, _err: str, _extra: int | None) -> None:
    os.setsid()
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                "print(os.getpid(),flush=True); time.sleep(60)"
            ),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None
    descendant = int(child.stdout.readline())
    ready = Path(Path(src).read_text())
    pending = ready.with_suffix(".pending")
    pending.write_text(json.dumps([os.getpid(), descendant]))
    pending.rename(ready)
    signal.pause()


def wait_for_ready(ready: Path) -> None:
    deadline = time.monotonic() + 10
    while not ready.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("spawned worker did not become ready")
        time.sleep(0.01)


async def test_cancel_reaps_real_spawned_group_including_term_ignoring_descendant(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given
    ready = tmp_path / "ready.json"
    children_before = {child.pid for child in mp.active_children()}
    monkeypatch.setattr(runtime, "convert_worker", descendant_worker)
    task = asyncio.create_task(runtime.convert_document_isolated_async(str(ready).encode(), "report.docx"))
    try:
        await asyncio.to_thread(wait_for_ready, ready)
        leader, descendant = TypeAdapter(tuple[int, int]).validate_json(ready.read_text(), strict=True)
        try:
            # When
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 8)
            # Then
            with pytest.raises(ProcessLookupError):
                os.kill(leader, 0)
            with pytest.raises(ProcessLookupError):
                os.kill(descendant, 0)
            assert {child.pid for child in mp.active_children()} == children_before
            assert not runtime._QUARANTINED_JOBS
            assert runtime._CONVERT_PERMITS.acquire(blocking=False)
            assert runtime._CONVERT_PERMITS.acquire(blocking=False)
            runtime._CONVERT_PERMITS.release()
            runtime._CONVERT_PERMITS.release()
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(leader, signal.SIGKILL)
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
