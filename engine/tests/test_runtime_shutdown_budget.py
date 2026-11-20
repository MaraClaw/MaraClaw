"""``Runtime.stop`` ends inside one cleanup budget when owned work hangs.

Stops run on an event loop whose clock jumps to the next timer whenever every task waits, so the real budget costs no
wall time and ``loop.time()`` deltas are exact whatever the machine's load. Hung work is a task that keeps running
through cancellation (``drain_on_cancel``) until the scenario releases it.
"""

import asyncio
import selectors
from collections.abc import Awaitable, Callable
from itertools import accumulate
from typing import override

import pytest
from pydantic import ValidationError

from app.api import background_tasks
from app.config import FARGATE_MAX_STOP_TIMEOUT_SECONDS, SHUTDOWN_KILL_MARGIN_SECONDS, Settings, get_settings
from app.runtime import lifecycle, shutdown_budget
from app.runtime.shutdown_budget import STEP_SHARES, TAIL_SECONDS, TAIL_SHARES, ShutdownBudget
from app.services import heartbeat, scheduler_tasks
from app.services.agent_setup_lock import drain_on_cancel
from producer_admission_fixtures import producer_admission

__all__ = ["producer_admission"]

RESOURCES = ["database", "redis", "llm", "outbound", "tavily", "mcp", "logging"]
# What the log names for each kind of hung work.
HUNG_NAMES = {
    "owned task": "stubborn-loop",
    "schedule run": "schedule-execution",
    "background task": "stubborn route work",
    "producer task": "heartbeat:stubborn",
    "producer drain": "shutdown: inbox wakes",
}


class VirtualClockLoop(asyncio.SelectorEventLoop):
    """Its clock stands still while callbacks run and jumps to the next timer when every task waits."""

    def __init__(self) -> None:
        self.now: float = 0.0
        super().__init__(_JumpWhenIdle(self))

    @override
    def time(self) -> float:
        return self.now


class _JumpWhenIdle(selectors.DefaultSelector):
    def __init__(self, loop: VirtualClockLoop) -> None:
        super().__init__()
        self._loop: VirtualClockLoop = loop

    @override
    def select(self, timeout: float | None = None) -> list[tuple[selectors.SelectorKey, int]]:
        ready = super().select(0)
        if ready or timeout == 0:
            return ready
        if timeout is None:
            raise RuntimeError("every task waits and no timer is due: this stop would never return")
        self._loop.now += timeout
        return ready


def record_warnings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    warnings: list[str] = []

    def warning(message: str, *args: object, **_kwargs: object) -> None:
        warnings.append(message.format(*args))

    monkeypatch.setattr(shutdown_budget.logger, "warning", warning)
    return warnings


def fake_resources(monkeypatch: pytest.MonkeyPatch, events: list[str], on_database: Callable[[], None]) -> None:
    def close(name: str) -> Callable[[], Awaitable[None]]:
        async def record() -> None:
            events.append(name)

        return record

    async def close_pool() -> None:
        on_database()
        events.append("database")

    monkeypatch.setattr(lifecycle, "close_pool", close_pool)
    monkeypatch.setattr(lifecycle, "close_redis", close("redis"))
    monkeypatch.setattr(lifecycle, "shutdown_llm_clients", close("llm"))
    monkeypatch.setattr(lifecycle, "shutdown_outbound_client", close("outbound"))
    monkeypatch.setattr(lifecycle, "shutdown_tavily_client", close("tavily"))
    monkeypatch.setattr(lifecycle, "shutdown_mcp_sessions", close("mcp"))
    monkeypatch.setattr(lifecycle, "shutdown_logging", lambda: events.append("logging"))


@pytest.mark.parametrize(
    "hangs",
    [
        *({kind} for kind in HUNG_NAMES),
        set(HUNG_NAMES),
    ],
    ids=[*HUNG_NAMES, "everything at once"],
)
def test_stop_ends_within_budget_naming_what_hangs_and_closing_every_resource(
    monkeypatch: pytest.MonkeyPatch, hangs: set[str]
) -> None:
    # Given work of each kind that keeps running through cancellation, and resources that record their close.
    events: list[str] = []
    warnings = record_warnings(monkeypatch)
    named_before_database: list[str] = []
    fake_resources(monkeypatch, events, lambda: named_before_database.extend(warnings))

    async def drain_forever(grace_seconds: float) -> None:
        assert grace_seconds > 0
        await asyncio.Event().wait()

    if "producer drain" in hangs:
        monkeypatch.setattr(lifecycle, "drain_wake_tasks", drain_forever)
    stubborn_kinds = hangs - {"producer drain"}

    async def stop() -> tuple[float, list[asyncio.Task[object]], list[asyncio.Task[object]]]:
        loop = asyncio.get_running_loop()
        release, all_started = asyncio.Event(), asyncio.Event()
        hung: list[asyncio.Task[object]] = []

        async def stubborn() -> None:
            task = asyncio.current_task()
            assert task is not None
            hung.append(task)
            if len(hung) == len(stubborn_kinds):
                all_started.set()
            _ = await drain_on_cancel(release.wait())

        runtime = lifecycle.Runtime()
        runtime.pool_attempted = runtime.logging_attempted = True
        if "owned task" in hangs:
            runtime.tasks.start("stubborn-loop", stubborn(), requirement="loop")
        if "schedule run" in hangs:
            scheduler_tasks.start_schedule_tasks()
            assert await scheduler_tasks.schedule_execution(stubborn)
        if "background task" in hangs:
            background_tasks.start_background_tasks()
            background_tasks.schedule_background_task(stubborn(), "stubborn route work")
        if "producer task" in hangs:
            task = asyncio.create_task(stubborn(), name="heartbeat:stubborn")
            heartbeat._HEARTBEAT_TASKS.add(task)
            task.add_done_callback(heartbeat._HEARTBEAT_TASKS.discard)
        if stubborn_kinds:
            await all_started.wait()
        # When
        started = loop.time()
        await runtime.stop()
        elapsed = loop.time() - started
        running = [task for task in hung if not task.done()]
        release.set()
        _ = await asyncio.gather(*hung, return_exceptions=True)
        return elapsed, hung, running

    try:
        elapsed, hung, running = asyncio.run(stop(), loop_factory=VirtualClockLoop)
    finally:
        background_tasks.start_background_tasks()
    # Then the stop waited for the hung work only until a deadline inside the budget, and left it running.
    assert 0 < elapsed <= get_settings().SHUTDOWN_CLEANUP_BUDGET_SECONDS
    assert running == hung
    # And every resource closed in order, after the log named each abandoned task.
    assert events == RESOURCES
    for kind in hangs:
        assert any(HUNG_NAMES[kind] in line for line in named_before_database), (kind, named_before_database)


def test_each_drain_waits_half_its_slice_before_it_cancels(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given drains that record the grace they are given and return at once.
    events: list[str] = []
    fake_resources(monkeypatch, events, lambda: None)
    graces: dict[str, float] = {}

    def drain(step: str) -> Callable[..., Awaitable[None]]:
        async def record(grace_seconds: float) -> None:
            graces[step] = grace_seconds

        return record

    monkeypatch.setattr(lifecycle, "drain_background_tasks", drain("route background tasks"))
    monkeypatch.setattr(lifecycle, "drain_trigger_tasks", drain("trigger invocations"))
    monkeypatch.setattr(lifecycle, "drain_heartbeat_tasks", drain("heartbeats"))
    monkeypatch.setattr(lifecycle, "drain_wake_tasks", drain("inbox wakes"))

    async def stop() -> None:
        runtime = lifecycle.Runtime()
        monkeypatch.setattr(runtime.incidents, "stop", drain("incident alerts"))
        await runtime.stop()

    # When the stop runs on a clock that stands still while nothing waits.
    asyncio.run(stop(), loop_factory=VirtualClockLoop)
    # Then each drain waits half the time left until its scheduled end, here its share plus every earlier share.
    ends = dict(zip(STEP_SHARES, accumulate(STEP_SHARES.values()), strict=True))
    assert graces == {
        step: ends[step] / 2
        for step in ("route background tasks", "trigger invocations", "heartbeats", "inbox wakes", "incident alerts")
    }


def test_a_step_never_takes_the_time_reserved_for_the_steps_after_it() -> None:
    # Given the default budget on a fake clock, whose pre-tail shares fill what the tail leaves.
    now = 0.0
    budget = ShutdownBudget(75, lambda: now)
    assert sum(STEP_SHARES.values()) + TAIL_SECONDS == 75
    # Then a step that starts on time gets its share, and a group splits what is left evenly.
    assert budget.slice("route background tasks") == STEP_SHARES["route background tasks"]
    now = sum(STEP_SHARES[step] for step in ("route background tasks", "wrappers", "schedule executions"))
    assert budget.slice("connector managers", parts=5) == pytest.approx(STEP_SHARES["connector managers"] / 5)
    # And time an earlier step left unused carries over to the next one.
    now = 0.0
    assert budget.slice("wrappers") == STEP_SHARES["route background tasks"] + STEP_SHARES["wrappers"]
    # When every step before the tail overran, those steps get nothing and the tail keeps its reserve.
    now = 75 - TAIL_SECONDS + 1
    assert budget.slice("incident alerts") == 0
    assert budget.slice("database") == TAIL_SHARES["database"] - 1
    now = 75 - TAIL_SHARES["logging"]
    assert budget.slice("MCP sessions") == 0
    # And a smaller budget shrinks every share before the tail in proportion, never the tail.
    now = 0.0
    half = ShutdownBudget(TAIL_SECONDS + sum(STEP_SHARES.values()) / 2, lambda: now)
    assert half.slice("route background tasks") == STEP_SHARES["route background tasks"] / 2
    now = half.slice("incident alerts")
    assert now == sum(STEP_SHARES.values()) / 2
    assert half.slice("database") == TAIL_SHARES["database"]


def test_settings_keep_the_stop_window_inside_fargate_stop_timeout() -> None:
    fields = Settings.model_fields
    grace = fields["SHUTDOWN_HTTP_GRACE_SECONDS"].default
    budget = fields["SHUTDOWN_CLEANUP_BUDGET_SECONDS"].default
    assert grace + budget + SHUTDOWN_KILL_MARGIN_SECONDS <= FARGATE_MAX_STOP_TIMEOUT_SECONDS
    exactly_full = FARGATE_MAX_STOP_TIMEOUT_SECONDS - budget - SHUTDOWN_KILL_MARGIN_SECONDS
    assert exactly_full == Settings(SHUTDOWN_HTTP_GRACE_SECONDS=exactly_full).SHUTDOWN_HTTP_GRACE_SECONDS
    for invalid in (
        {"SHUTDOWN_HTTP_GRACE_SECONDS": exactly_full + 1},
        {"SHUTDOWN_HTTP_GRACE_SECONDS": 0},
        {"SHUTDOWN_CLEANUP_BUDGET_SECONDS": TAIL_SECONDS - 1},
    ):
        with pytest.raises(ValidationError):
            _ = Settings.model_validate(invalid)
