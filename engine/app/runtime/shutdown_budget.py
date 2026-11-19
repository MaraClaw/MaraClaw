"""One cleanup budget for ``Runtime.stop``: per-step slices, a reserved tail, and joins that stop at a deadline.

Every step has a scheduled end inside the budget and may run until it, so time an earlier step left unused carries
over, while an overrun eats only into the steps before the tail. The tail's ends stay fixed against the end of the
budget, so the resource closes and ``shutdown_logging`` are always reached. Inside a step, joins of cancelled work
stop waiting at the deadline the runtime set for the step, log the names of the tasks still running, and return.
They never cancel the tasks a second time and never cancel their own waiter, which ``drain_on_cancel`` would absorb.
"""

import asyncio
from collections.abc import Callable, Collection, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from itertools import accumulate
from types import CoroutineType

from app.core.logging import logger

# Seconds for each step before the tail, in ``Runtime.stop`` order, at the default 75 s budget. Another budget scales
# them in proportion. Drains wait half of their slice for work to finish before they cancel it.
STEP_SHARES: dict[str, float] = {
    "route background tasks": 14,
    "wrappers": 4,
    "schedule executions": 6,
    "connector managers": 8,
    "trigger invocations": 6,
    "heartbeats": 6,
    "inbox wakes": 6,
    "ss-local": 4,
    "realtime": 2,
    "incident alerts": 4,
}
# Seconds reserved at the end of every budget for the shared resources, whatever the steps before them took.
TAIL_SHARES: dict[str, float] = {
    "database": 3,
    "redis": 2,
    "LLM clients": 2,
    "outbound HTTP client": 2,
    "Tavily HTTP client": 1,
    "MCP sessions": 3,
    "logging": 2,
}
TAIL_SECONDS: float = sum(TAIL_SHARES.values())
# A step's joins stop this fraction of its slice early, so a step that abandons tasks still names them and returns
# before the runtime stops waiting for the step itself.
SETTLE_FRACTION = 0.1

_deadline: ContextVar[float | None] = ContextVar("shutdown_join_deadline", default=None)


class ShutdownBudget:
    """Scheduled step ends, ``total`` seconds from the clock reading at creation."""

    def __init__(self, total: float, clock: Callable[[], float]) -> None:
        self._clock: Callable[[], float] = clock
        scale = (total - TAIL_SECONDS) / sum(STEP_SHARES.values())
        shares = {**{step: share * scale for step, share in STEP_SHARES.items()}, **TAIL_SHARES}
        start = clock()
        self._ends: dict[str, float] = {
            step: start + end for step, end in zip(shares, accumulate(shares.values()), strict=True)
        }

    def slice(self, step: str, parts: int = 1) -> float:
        """Seconds ``step`` may take from now; ``parts`` steps that share one end split what is left evenly."""
        return max(0.0, (self._ends[step] - self._clock()) / parts)


@contextmanager
def joins_until(deadline: float) -> Generator[None]:
    """Make joins in this context, and in tasks created inside it, stop waiting at ``deadline`` (event-loop time)."""
    token = _deadline.set(deadline)
    try:
        yield
    finally:
        _deadline.reset(token)


async def join(tasks: Collection[asyncio.Task[object]], label: str) -> None:
    """Wait for ``tasks`` until the ambient shutdown deadline, or until all finish outside a shutdown step."""
    if not tasks:
        return
    deadline = _deadline.get()
    if deadline is None:
        _ = await asyncio.wait(tasks)
        return
    _, pending = await asyncio.wait(tasks, timeout=max(0.0, deadline - asyncio.get_running_loop().time()))
    if pending:
        abandoned(label, pending)


def abandoned(label: str, tasks: Collection[asyncio.Task[object]]) -> None:
    """Name the tasks a shutdown step leaves running when its deadline passes."""
    names = ", ".join(sorted(_describe(task) for task in tasks))
    logger.warning("[shutdown] {} still running at the deadline; abandoning {}: {}", label, len(tasks), names)


def _describe(task: asyncio.Task[object]) -> str:
    coroutine = task.get_coro()
    if isinstance(coroutine, CoroutineType):
        return f"{task.get_name()} ({coroutine.__qualname__})"
    return task.get_name()
