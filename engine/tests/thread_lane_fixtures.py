"""Guards that fail a test when work reaches the event loop's default executor or anyio's worker threads."""

import asyncio
from collections.abc import Generator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from typing import override

import anyio
import pytest


class _ForbiddenExecutor(ThreadPoolExecutor):
    @override
    def submit(self, fn, /, *args, **kwargs) -> Future[object]:
        raise AssertionError("work reached the default executor")


@contextmanager
def forbid_shared_threads(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    """Inside the block `asyncio.to_thread`, `run_in_executor(None, ...)` and `anyio.to_thread.run_sync` raise."""

    async def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("work reached anyio's worker threads")

    loop = asyncio.get_running_loop()
    executor = _ForbiddenExecutor(max_workers=1)
    loop.set_default_executor(executor)
    with monkeypatch.context() as patch:
        patch.setattr(anyio.to_thread, "run_sync", forbidden)
        try:
            yield
        finally:
            loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
            executor.shutdown()
