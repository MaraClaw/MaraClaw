"""Event-loop lag sampler: a timer that wakes later than scheduled shows how long the loop was blocked."""

import asyncio
from collections.abc import Awaitable, Callable
from time import monotonic

from app.core.metrics import observe_event_loop_lag
from app.runtime.tasks import loop_progress

# One sample per ``EVENT_LOOP_LAG_INTERVAL_SECONDS`` (at most 60), beating after each.
PROGRESS_BUDGET_SECONDS = 300.0


async def sample_event_loop_lag(
    interval_seconds: float,
    *,
    clock: Callable[[], float] = monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Every interval, record how much later than due the loop resumed this task, then beat."""
    while True:
        due = clock() + interval_seconds
        await sleep(interval_seconds)
        observe_event_loop_lag(max(0.0, clock() - due))
        loop_progress().beat()
