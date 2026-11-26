import asyncio

import pytest

from app.runtime.tasks import (
    RESTART_BASE_SECONDS,
    OwnedTasks,
    ReconnectBackoff,
    RestartPolicy,
    capped_backoff,
    jittered_backoff,
)
from worker_progress_fixtures import Incarnations, Pacing


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def fixed(value: float):
    return lambda: value


@pytest.mark.parametrize(("base", "cap"), [(2.0, 60.0), (5.0, 120.0), (30.0, 300.0), (1.0, 1.0)])
@pytest.mark.parametrize("value", [0.0, 0.5, 1.0, -3.0, 7.5])
def test_jittered_backoff_stays_within_base_and_cap_for_any_random_value(base: float, cap: float, value: float) -> None:
    # Given any failure count and a random source, including values outside [0, 1].
    for failures in range(41):
        delay = jittered_backoff(failures, base_seconds=base, cap_seconds=cap, random=fixed(value))
        # Then the delay never leaves [base, cap].
        assert base <= delay <= cap


def test_jittered_backoff_draws_from_a_window_that_doubles_up_to_the_cap() -> None:
    lows = [jittered_backoff(n, base_seconds=2, cap_seconds=60, random=fixed(0.0)) for n in range(41)]
    highs = [jittered_backoff(n, base_seconds=2, cap_seconds=60, random=fixed(1.0)) for n in range(41)]
    # The first retry is already spread, and each window starts where the deterministic doubling would be.
    assert lows[:7] == [2, 4, 8, 16, 30, 30, 30]
    assert highs[:7] == [4, 8, 16, 32, 60, 60, 60]
    # Neither end of the window ever drops as failures grow, and the cap keeps a spread.
    assert lows == sorted(lows)
    assert highs == sorted(highs)
    assert (lows[-1], highs[-1]) == (30, 60)


def test_jittered_backoff_moves_through_the_window_with_the_random_value() -> None:
    delays = [jittered_backoff(3, base_seconds=2, cap_seconds=60, random=fixed(value)) for value in (0, 0.25, 0.5, 1)]
    assert delays == [16, 20, 24, 32]


def test_jittered_backoff_defaults_to_the_system_random_source() -> None:
    assert all(16 <= jittered_backoff(3, base_seconds=2, cap_seconds=60) <= 32 for _ in range(32))


@pytest.mark.parametrize(("base", "cap"), [(0.0, 10.0), (-1.0, 10.0), (5.0, 4.0)])
def test_jittered_backoff_rejects_a_non_positive_base_or_a_cap_below_it(base: float, cap: float) -> None:
    with pytest.raises(ValueError, match="base_seconds"):
        _ = jittered_backoff(0, base_seconds=base, cap_seconds=cap)


def test_capped_backoff_keeps_its_deterministic_doubling() -> None:
    assert [capped_backoff(n, base_seconds=1, cap_seconds=30) for n in range(8)] == [1, 2, 4, 8, 16, 30, 30, 30]
    assert [capped_backoff(n, base_seconds=5, cap_seconds=120) for n in range(7)] == [5, 10, 20, 40, 80, 120, 120]
    assert capped_backoff(10_000, base_seconds=1, cap_seconds=30) == 30


def policy(clock: FakeClock, value: float = 0.0) -> ReconnectBackoff:
    return ReconnectBackoff(base_seconds=2.0, cap_seconds=60.0, healthy_seconds=60.0, random=fixed(value), clock=clock)


def test_reconnect_backoff_escalates_while_runs_keep_failing_fast() -> None:
    clock = FakeClock()
    backoff = policy(clock)
    delays = [backoff.next_delay(backoff.started()) for _ in range(7)]
    assert delays == [2, 4, 8, 16, 30, 30, 30]
    assert backoff.failures == 7


@pytest.mark.parametrize(("run_seconds", "starts_over"), [(59.9, False), (60.0, True), (3600.0, True)])
def test_reconnect_backoff_starts_over_only_after_a_run_reaches_the_healthy_threshold(
    run_seconds: float, starts_over: bool
) -> None:
    # Given three fast failures.
    clock = FakeClock()
    backoff = policy(clock)
    for _ in range(3):
        _ = backoff.next_delay(backoff.started())
    # When the next run lasts run_seconds on the monotonic clock before it ends.
    started = backoff.started()
    clock.now += run_seconds
    delay = backoff.next_delay(started)
    # Then escalation restarted only if that run was healthy.
    assert delay == (2 if starts_over else 16)
    assert backoff.failures == (1 if starts_over else 4)


def test_reconnect_backoff_started_reads_the_injected_clock() -> None:
    clock = FakeClock()
    clock.now = 41.5
    assert policy(clock).started() == 41.5


def test_reconnect_backoff_settle_clears_failures_after_a_healthy_run_without_scheduling_a_delay() -> None:
    clock = FakeClock()
    backoff = policy(clock)
    for _ in range(3):
        _ = backoff.next_delay(backoff.started())
    started = backoff.started()
    clock.now = 59.9
    backoff.settle(started)
    assert backoff.failures == 3
    clock.now = 60.0
    backoff.settle(started)
    assert backoff.failures == 0


@pytest.mark.parametrize("ceiling", [0.5, 4.0, 30.0])
async def test_restart_delays_are_capped_backoff_up_to_the_policy_ceiling(ceiling: float) -> None:
    # Given a loop that crashes through a budget of seven restarts.
    budget = 7
    pacing = Pacing()
    tasks = OwnedTasks(pacing.clock, pacing.wait)
    tasks.start_supervised(
        "mail",
        Incarnations(*(RuntimeError("crash") for _ in range(budget + 1))),
        progress_budget_seconds=300,
        policy=RestartPolicy(budget, ceiling),
    )
    supervisor = next(task for task in tasks.tasks if task.get_name() == "mail-supervisor")
    # When every backoff is released in turn.
    delays: list[float] = []
    for _ in range(budget):
        delays.append(await pacing.next_wait())
        pacing.release()
    _ = await asyncio.wait_for(supervisor, 2)
    # Then each delay is the shared deterministic backoff, which never exceeds the ceiling.
    assert delays == [capped_backoff(n, base_seconds=RESTART_BASE_SECONDS, cap_seconds=ceiling) for n in range(budget)]
    assert max(delays) == min(ceiling, 64.0)
    assert delays[:7] == {0.5: [0.5] * 7, 4.0: [1, 2, 4, 4, 4, 4, 4], 30.0: [1, 2, 4, 8, 16, 30, 30]}[ceiling]
    await tasks.stop()
