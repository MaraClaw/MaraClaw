"""Local workspace exclusion shared by setup writers and deletion processes."""

import asyncio
import fcntl
from collections.abc import AsyncGenerator, Coroutine
from contextlib import asynccontextmanager
from pathlib import Path

import anyio


@asynccontextmanager
async def setup_workspace_lock(agent_dir: Path, *, shared: bool = False) -> AsyncGenerator[None]:
    """Exclusive by default; ``shared`` lets concurrent guest-visible writers coexist and only fences exclusive holders."""
    mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
    lock_dir = agent_dir.parent / "_setup_locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    with (lock_dir / f"{agent_dir.name}.lock").open("a+b") as lock:
        while True:
            try:
                fcntl.flock(lock, mode | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await anyio.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


async def drain_on_cancel[Result](operation: Coroutine[object, object, Result]) -> Result:
    task = asyncio.create_task(operation)
    cancelled = False
    with anyio.CancelScope(shield=True):
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            if not task.cancelled():
                _ = task.exception()
            raise asyncio.CancelledError
    return task.result()
