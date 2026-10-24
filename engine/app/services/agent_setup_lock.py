"""Local workspace exclusion shared by setup writers and deletion processes."""

import asyncio
import fcntl
from collections.abc import AsyncGenerator, Coroutine
from contextlib import asynccontextmanager
from pathlib import Path

import anyio


class WorkspaceLockTimeoutError(TimeoutError):
    """The workspace fence could not be acquired within its wait budget."""


@asynccontextmanager
async def setup_workspace_lock(
    agent_dir: Path, *, shared: bool = False, timeout_seconds: float | None = None
) -> AsyncGenerator[None]:
    """Exclusive by default; ``shared`` lets concurrent guest-visible writers coexist and only fences exclusive holders."""
    mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
    lock_dir = agent_dir.parent / "_setup_locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    with (lock_dir / f"{agent_dir.name}.lock").open("a+b") as lock:
        deadline = None if timeout_seconds is None else anyio.current_time() + timeout_seconds
        while True:
            try:
                fcntl.flock(lock, mode | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                remaining = None if deadline is None else deadline - anyio.current_time()
                if remaining is not None and remaining <= 0:
                    raise WorkspaceLockTimeoutError("Timed out waiting for the workspace lock.") from exc
                await anyio.sleep(0.05 if remaining is None else min(0.05, remaining))
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
