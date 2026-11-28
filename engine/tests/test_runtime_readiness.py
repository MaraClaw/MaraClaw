import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.core import events, metrics
from app.process_roles import parse_process_roles
from app.runtime import lifecycle, loop_lag, readiness
from app.runtime.tasks import OwnedTasks, RestartPolicy, loop_progress
from producer_admission_fixtures import producer_admission
from worker_progress_fixtures import Incarnations, Pacing, WorkerHTTP, worker_http

__all__ = ["producer_admission", "worker_http"]


@dataclass
class Probes:
    database: bool = True
    redis: bool = True
    calls: int = 0


@pytest.fixture
def probes(monkeypatch: pytest.MonkeyPatch) -> Probes:
    state = Probes()

    async def database() -> bool:
        state.calls += 1
        return state.database

    async def redis() -> bool:
        state.calls += 1
        return state.redis

    monkeypatch.setattr(readiness, "ping_pool", database)
    monkeypatch.setattr(readiness, "ping_redis", redis)
    monkeypatch.setattr(readiness, "realtime_router", SimpleNamespace(ready=True))
    return state


@pytest.mark.parametrize("role", ["api", "worker", "connector", "worker,connector", "api,worker", "all", "bootstrap"])
async def test_role_readiness_requires_startup_and_all_shared_dependencies(role: str, probes: Probes) -> None:
    state = readiness.Readiness(OwnedTasks())
    state.roles = parse_process_roles(role)
    assert not await state.check()
    assert probes.calls == 0
    state.initialized = True
    assert await state.check() == (role != "bootstrap")
    probes.database = False
    assert not await state.check()
    probes.database, probes.redis = True, False
    assert not await state.check()
    probes.redis = True
    state.draining = True
    assert not await state.check()


@pytest.mark.parametrize("outcome", ["return", "error", "cancel"])
async def test_required_loop_retains_unexpected_outcomes(outcome: str) -> None:
    tasks = OwnedTasks()
    entered, release = asyncio.Event(), asyncio.Event()

    async def loop() -> None:
        entered.set()
        await release.wait()
        if outcome == "error":
            raise RuntimeError("required loop failed")

    tasks.start("required", loop(), requirement="loop")
    await entered.wait()
    assert tasks.ready()
    task, _ = tasks.required["required"]
    if outcome == "cancel":
        task.cancel()
    else:
        release.set()
    await asyncio.gather(task, return_exceptions=True)
    assert not tasks.ready()
    assert "required" in tasks.required
    # Nothing restarts a plain required loop, so its exit is final.
    assert tasks.exhausted == {"required"}
    await tasks.stop()


class FakeClock:
    def __init__(self) -> None:
        self.now: float = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_budgeted_loop_is_stale_without_beats_and_recovers_on_a_beat() -> None:
    # Given a running budgeted loop and an unbudgeted sibling on a fake monotonic clock.
    clock = FakeClock()
    tasks = OwnedTasks(clock)
    entered = asyncio.Event()

    async def budgeted() -> None:
        entered.set()
        await asyncio.Event().wait()

    tasks.start("budgeted", budgeted(), requirement="loop", progress_budget_seconds=30)
    tasks.start("plain", asyncio.Event().wait(), requirement="loop")
    await entered.wait()
    # When time stays within the budget, then passes it without a beat.
    clock.now += 30
    assert tasks.ready()
    clock.now += 0.5
    # Then only the budgeted loop makes readiness fail, and its task is untouched.
    assert not tasks.ready()
    assert not tasks.required["budgeted"][0].done()
    # When the loop beats, readiness recovers for another full budget.
    tasks.progress["budgeted"].beat()
    assert tasks.ready()
    clock.now += 31
    assert not tasks.ready()
    await tasks.stop()


async def test_loop_progress_reaches_only_the_budgeted_task_and_exempt_time_is_not_staleness() -> None:
    # Given a budgeted loop that beats and enters an exempt section.
    clock = FakeClock()
    tasks = OwnedTasks(clock)
    inside, release, left = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def loop() -> None:
        loop_progress().beat()
        with loop_progress().exempt():
            inside.set()
            await release.wait()
        left.set()
        await asyncio.Event().wait()

    tasks.start("budgeted", loop(), requirement="loop", progress_budget_seconds=30)
    await inside.wait()
    # When far more than the budget elapses inside the exempt section.
    clock.now += 1000
    assert tasks.ready()
    # Then leaving it restarts the budget instead of reporting a stall.
    release.set()
    await left.wait()
    assert tasks.ready()
    clock.now += 31
    assert not tasks.ready()
    assert loop_progress().stale() is False
    await tasks.stop()


async def test_progress_budget_is_only_for_required_loops() -> None:
    coroutine = asyncio.sleep(0)
    with pytest.raises(ValueError, match="required loops"):
        OwnedTasks().start("setup", coroutine, requirement="setup", progress_budget_seconds=1)
    _ = await coroutine


@pytest.mark.parametrize("outcome", ["return", "error", "cancel"])
async def test_finite_setup_requires_successful_completion(outcome: str) -> None:
    tasks = OwnedTasks()
    release = asyncio.Event()

    async def setup() -> None:
        await release.wait()
        if outcome == "error":
            raise RuntimeError("setup failed")

    tasks.start("connector", setup(), requirement="setup")
    assert not tasks.ready()
    task, _ = tasks.required["connector"]
    if outcome == "cancel":
        task.cancel()
    else:
        release.set()
    await asyncio.gather(task, return_exceptions=True)
    assert tasks.ready() == (outcome == "return")
    await tasks.stop()


async def test_probe_race_rechecks_drain_and_subscription(monkeypatch: pytest.MonkeyPatch, probes: Probes) -> None:
    state = readiness.Readiness(OwnedTasks())
    state.roles, state.initialized = frozenset({"api"}), True
    entered, release = asyncio.Event(), asyncio.Event()

    async def database() -> bool:
        entered.set()
        await release.wait()
        return True

    monkeypatch.setattr(readiness, "ping_pool", database)
    check = asyncio.create_task(state.check())
    await entered.wait()
    state.draining = True
    release.set()
    assert not await check


async def test_existing_redis_probe_never_initializes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(events, "_redis_client", None)

    async def forbidden() -> None:
        pytest.fail("readiness initialized Redis")

    monkeypatch.setattr(events, "get_redis", forbidden)
    assert not await events.ping_redis()


@pytest.mark.parametrize("failure", [RuntimeError("redis://secret@private"), TimeoutError("private-host")])
async def test_http_dependency_errors_are_unready_and_secret_free(
    monkeypatch: pytest.MonkeyPatch, probes: Probes, failure: Exception
) -> None:
    application = FastAPI()
    application.include_router(readiness.router)
    state = readiness.Readiness(OwnedTasks())
    state.roles, state.initialized = frozenset({"worker"}), True
    application.state.readiness = state

    async def failed() -> bool:
        raise failure

    monkeypatch.setattr(readiness, "ping_redis", failed)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        response = await client.get("/api/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


async def test_uninitialized_http_poll_does_not_start_runtime(probes: Probes) -> None:
    application = FastAPI()
    application.include_router(readiness.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        response = await client.get("/api/ready")
    assert response.status_code == 503
    assert probes.calls == 0


async def test_http_readiness_transitions_and_database_health_compatibility(
    monkeypatch: pytest.MonkeyPatch, probes: Probes
) -> None:
    from app.db import pool
    from app.main import app

    async def database() -> bool:
        return True

    monkeypatch.setattr(pool, "ping_pool", database)
    state = readiness.Readiness(OwnedTasks())
    state.roles = frozenset({"api"})
    monkeypatch.setattr(app.state, "readiness", state, raising=False)
    subscriber = SimpleNamespace(ready=False)
    monkeypatch.setattr(readiness, "realtime_router", subscriber)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/health")).status_code == 200
        assert (await client.get("/api/ready")).status_code == 503
        state.initialized = True
        assert (await client.get("/api/ready")).status_code == 503
        subscriber.ready = True
        assert (await client.get("/api/ready")).status_code == 200
        subscriber.ready = False
        assert (await client.get("/api/ready")).status_code == 503
        subscriber.ready = True
        assert (await client.get("/api/ready")).status_code == 200
        probes.redis = False
        assert (await client.get("/api/ready")).json() == {"status": "not_ready"}
        probes.redis = True
        state.draining = True
        assert (await client.get("/api/ready")).status_code == 503
        assert (await client.get("/api/health")).status_code == 200


@pytest.mark.parametrize("cancel", [False, True])
async def test_partial_lifespan_and_shutdown_clear_runtime_evidence(
    monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    application = FastAPI()
    observed: list[bool] = []

    async def start(runtime: lifecycle.Runtime, _roles: frozenset[str]) -> None:
        observed.append(runtime.readiness.initialized)
        if cancel:
            raise asyncio.CancelledError
        raise RuntimeError("partial startup")

    async def stop(runtime: lifecycle.Runtime) -> None:
        observed.append(runtime.readiness.draining)

    monkeypatch.setattr(lifecycle.Runtime, "start", start)
    monkeypatch.setattr(lifecycle.Runtime, "stop", stop)
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        async with lifecycle.lifespan(application):
            pytest.fail("partial startup yielded")
    assert observed == [False, True]


async def test_repeated_lifespans_allocate_fresh_runtime_state(monkeypatch: pytest.MonkeyPatch) -> None:
    application = FastAPI()
    states: list[readiness.Readiness] = []

    async def start(runtime: lifecycle.Runtime, _roles: frozenset[str]) -> None:
        states.append(runtime.readiness)
        assert not runtime.readiness.initialized
        runtime.readiness.initialized = True

    async def stop(_runtime: lifecycle.Runtime) -> None:
        return None

    monkeypatch.setattr(lifecycle.Runtime, "start", start)
    monkeypatch.setattr(lifecycle.Runtime, "stop", stop)
    for _ in range(2):
        async with lifecycle.lifespan(application):
            assert not application.state.readiness.draining
        assert application.state.readiness.draining
    assert states[0] is not states[1]


@pytest.mark.parametrize("blocked_scanner", ["start", "scheduler", "dispatch"])
async def test_http_observes_each_scanner_stall_while_its_siblings_keep_progressing(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, blocked_scanner: str
) -> None:
    from app.dao.schedule_dao import agent_schedule_dao
    from app.dao.schedule_manual_run_dao import schedule_manual_run_dao
    from app.services import agent_start_reconciler as starts, openclaw_dispatch_reconciler as dispatch, scheduler

    pacing = {name: Pacing() for name in ("start", "scheduler", "dispatch")}
    entered, release = asyncio.Event(), asyncio.Event()
    stalled = False

    async def dependency(name: str) -> None:
        if stalled and name == blocked_scanner:
            entered.set()
            await release.wait()

    async def outstanding() -> list[object]:
        await dependency("start")
        return []

    async def manual(*_args: object) -> list[object]:
        await dependency("scheduler")
        return []

    async def empty(*_args: object) -> list[object]:
        return []

    async def pending(*_args: object, **_kwargs: object) -> tuple[list[object], None]:
        await dependency("dispatch")
        return [], None

    async def delivered(*_args: object, **_kwargs: object) -> tuple[list[object], None]:
        return [], None

    @asynccontextmanager
    async def connection():
        yield None

    monkeypatch.setattr(starts.start_store, "outstanding", outstanding)
    monkeypatch.setattr(starts, "sleep", pacing["start"].wait)
    monkeypatch.setattr(scheduler, "free_schedule_capacity", lambda: 2)
    monkeypatch.setattr(scheduler, "connection_ctx", connection)
    monkeypatch.setattr("app.services.schedule_manual.connection_ctx", connection)
    monkeypatch.setattr(schedule_manual_run_dao, "claim_pending", manual)
    monkeypatch.setattr(agent_schedule_dao, "claim_due", empty)
    monkeypatch.setattr(scheduler.asyncio, "sleep", pacing["scheduler"].wait)
    monkeypatch.setattr(dispatch.gateway_message_dao, "pending_dispatch_page", pending)
    monkeypatch.setattr(dispatch.gateway_message_dao, "delivered_dispatch_page", delivered)
    monkeypatch.setattr(dispatch.agent_dao, "get_many", empty)
    monkeypatch.setattr(dispatch.anyio, "sleep", pacing["dispatch"].wait)
    loops = {
        "start": starts.agent_start_reconciler_loop(),
        "scheduler": scheduler.start_scheduler(),
        "dispatch": dispatch.openclaw_dispatch_reconciler_loop(),
    }
    for name, loop in loops.items():
        worker_http.runtime.tasks.start(name, loop, requirement="loop", progress_budget_seconds=300)
    for control in pacing.values():
        await control.paused()
    await worker_http.ready(expected=True)
    stalled = True
    for control in pacing.values():
        control.go.put_nowait(None)
    await asyncio.wait_for(entered.wait(), 2)
    for name, control in pacing.items():
        if name != blocked_scanner:
            await control.paused()
    worker_http.pacing.now += 301
    for name, control in pacing.items():
        if name != blocked_scanner:
            control.go.put_nowait(None)
            await control.paused()
    await worker_http.ready(expected=False)
    assert all(not task.done() for task, _ in worker_http.runtime.tasks.required.values())
    release.set()
    await pacing[blocked_scanner].paused()
    await worker_http.ready(expected=True)
    worker_http.runtime.begin_drain()
    await worker_http.ready(expected=False)
    await worker_http.runtime.tasks.stop()
    assert all(task.cancelled() for task, _ in worker_http.runtime.tasks.required.values())


def start_mail(tasks: OwnedTasks, incarnations: Incarnations, *, budget: int = 5) -> asyncio.Task[object]:
    tasks.start_supervised("mail", incarnations, progress_budget_seconds=300, policy=RestartPolicy(budget, 30.0))
    return next(task for task in tasks.tasks if task.get_name() == "mail-supervisor")


async def test_restarted_loop_is_unready_until_its_first_real_beat_each_time() -> None:
    # Given a loop that beats, is cancelled, and is restarted twice.
    pacing = Pacing()
    tasks = OwnedTasks(pacing.clock, pacing.wait)
    incarnations = Incarnations(RuntimeError("crash"), None, None)
    _ = start_mail(tasks, incarnations)
    progress = tasks.progress["mail"]
    # When the first restart is running: alive, within budget, but it has not produced progress yet.
    _ = await pacing.next_wait()
    pacing.release()
    await incarnations.running()
    assert not tasks.required["mail"][0].done()
    assert not progress.stale()
    assert not tasks.ready()
    # Then time spent in an exempt section is not a beat, and only a real one makes it ready.
    with progress.exempt():
        pass
    assert not tasks.ready()
    progress.beat()
    assert tasks.ready()
    # And the next crash puts the loop through the same gate again instead of flapping green.
    incarnations.tasks[-1].cancel()
    assert await pacing.next_wait() == 2.0
    assert not tasks.ready()
    pacing.release()
    await incarnations.running()
    assert not tasks.ready()
    progress.beat()
    assert tasks.ready()
    await tasks.stop()


async def test_stale_progress_is_reported_and_never_restarts_the_loop() -> None:
    # Given a supervised loop that never beats.
    pacing = Pacing()
    tasks = OwnedTasks(pacing.clock, pacing.wait)
    incarnations = Incarnations(None)
    _ = start_mail(tasks, incarnations)
    await incarnations.running()
    # When the progress budget passes.
    pacing.now += 301
    # Then it is unready but still the same running task: no restart, no backoff, no exhaustion.
    assert tasks.progress["mail"].stale()
    assert not tasks.ready()
    assert not tasks.required["mail"][0].done()
    assert len(incarnations.coroutines) == 1
    assert tasks.restarts == {"mail": 0}
    assert tasks.exhausted == set()
    assert pacing.entered.empty()
    await tasks.stop()


async def test_liveness_fails_only_when_a_loop_exhausted_its_restarts(
    worker_http: WorkerHTTP, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db import pool

    # Given dependencies that would fail, and a loop that crashes through a budget of one restart.
    async def failing() -> bool:
        raise AssertionError("liveness must not probe a dependency")

    monkeypatch.setattr(readiness, "ping_pool", failing)
    monkeypatch.setattr(readiness, "ping_redis", failing)
    monkeypatch.setattr(pool, "ping_pool", failing)
    await worker_http.live(expected=True)
    supervisor = start_mail(worker_http.runtime.tasks, Incarnations(RuntimeError("one"), RuntimeError("two")), budget=1)
    # When the loop is down and waiting for its restart: not ready, still alive.
    await worker_http.pacing.paused()
    assert (await worker_http.client.get("/api/ready")).status_code == 503
    await worker_http.live(expected=True)
    # Then the restart is spent on a second crash and the process is reported failed for good.
    worker_http.pacing.release()
    _ = await asyncio.wait_for(supervisor, 2)
    await worker_http.live(expected=False)
    assert (await worker_http.client.get("/api/ready")).status_code == 503


async def test_liveness_fails_when_a_loop_nothing_restarts_dies(worker_http: WorkerHTTP) -> None:
    # Given a running plain required loop.
    incarnations = Incarnations(None)
    task = worker_http.start_loop("plain", incarnations(), budget_seconds=300)
    await incarnations.running()
    await worker_http.live(expected=True)
    await worker_http.ready(expected=True)
    # When it dies outside shutdown.
    _ = task.cancel()
    _ = await asyncio.gather(task, return_exceptions=True)
    # Then the process is failed and not ready, with the unchanged response bodies.
    await worker_http.live(expected=False)
    await worker_http.ready(expected=False)


def recorded_warnings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The readiness warnings only: the process-wide logger also carries the supervisor's own."""
    messages: list[str] = []

    def record(message: str, *args: object) -> None:
        if message.startswith("[readiness]"):
            messages.append(message.format(*args))

    monkeypatch.setattr(readiness.logger, "warning", record)
    return messages


async def test_a_loop_turning_stale_logs_one_warning_per_red_transition(
    probes: Probes, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a ready worker whose budgeted loop is running.
    messages = recorded_warnings(monkeypatch)
    pacing = Pacing()
    tasks = OwnedTasks(pacing.clock, pacing.wait)
    state = readiness.Readiness(tasks)
    state.roles, state.initialized = frozenset({"worker"}), True
    incarnations = Incarnations(None)
    tasks.start("sampler", incarnations(), requirement="loop", progress_budget_seconds=60)
    await incarnations.running()
    assert await state.check()
    assert messages == []
    # When it goes stale and is probed over and over.
    pacing.now = 61
    for _ in range(3):
        assert not await state.check()
        assert state.alive()
    # Then one warning names the loop and why, and recovery plus a new stale spell logs once more.
    assert messages == ["[readiness] Required loop sampler is not ready: stale"]
    tasks.progress["sampler"].beat()
    assert await state.check()
    pacing.now = 200
    assert not await state.check()
    assert messages == ["[readiness] Required loop sampler is not ready: stale"] * 2
    await tasks.stop()


@pytest.mark.parametrize(("supervised", "reason"), [(True, "ended"), (False, "exhausted")])
async def test_the_warning_names_why_a_dead_loop_is_not_ready(
    probes: Probes, monkeypatch: pytest.MonkeyPatch, supervised: bool, reason: str
) -> None:
    # Given a loop that crashes, either waiting for its restart or with nothing to restart it.
    messages = recorded_warnings(monkeypatch)
    pacing = Pacing()
    tasks = OwnedTasks(pacing.clock, pacing.wait)
    state = readiness.Readiness(tasks)
    state.roles, state.initialized = frozenset({"worker"}), True
    incarnations = Incarnations(RuntimeError("crash"), None)
    if supervised:
        _ = start_mail(tasks, incarnations)
        _ = await pacing.next_wait()
    else:
        tasks.start("mail", incarnations(), requirement="loop", progress_budget_seconds=300)
        _ = await asyncio.gather(tasks.required["mail"][0], return_exceptions=True)
    # When it is probed more than once.
    assert not await state.check()
    assert not await state.check()
    # Then one warning names the loop and the reason.
    assert messages == [f"[readiness] Required loop mail is not ready: {reason}"]
    await tasks.stop()


async def test_loop_snapshots_follow_a_supervised_loop_through_restart_beat_and_staleness() -> None:
    # Given a supervised loop that crashes once, a plain loop, and a finite setup.
    pacing = Pacing()
    tasks = OwnedTasks(pacing.clock, pacing.wait)
    crashing, plain = Incarnations(RuntimeError("crash"), None), Incarnations(None)
    _ = start_mail(tasks, crashing)
    tasks.start("sampler", plain(), requirement="loop", progress_budget_seconds=60)
    tasks.start("setup", Incarnations("return")(), requirement="setup")
    _ = await pacing.next_wait()
    await plain.running()
    # When the crash waits for its restart: only the ongoing loops are reported, the mail loop as ended.
    pacing.now = 10
    mail, sampler = tasks.loop_snapshots()
    assert (mail.name, mail.reason, mail.restarts, mail.exhausted, mail.seconds_since_beat) == (
        "mail",
        "ended",
        0,
        False,
        10,
    )
    assert (sampler.name, sampler.reason, sampler.stale, sampler.seconds_since_beat) == ("sampler", None, False, 10)
    # And once restarted it stays ended until its first real beat, then goes stale with the budget.
    pacing.release()
    await crashing.running()
    assert tasks.loop_snapshots()[0].reason == "ended"
    assert tasks.loop_snapshots()[0].restarts == 1
    tasks.progress["mail"].beat()
    assert tasks.loop_snapshots()[0].ready
    pacing.now = 10 + 301
    stale = tasks.loop_snapshots()[0]
    assert (stale.reason, stale.stale, stale.seconds_since_beat) == ("stale", True, 301)
    await tasks.stop()


async def test_liveness_answers_without_dependency_probes_or_runtime_state(probes: Probes) -> None:
    # Given an eligible worker whose pool and Redis probes are down.
    state = readiness.Readiness(OwnedTasks())
    state.roles, state.initialized = frozenset({"worker"}), True
    probes.database = probes.redis = False
    application = FastAPI()
    application.include_router(readiness.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        # When asked before the lifespan published any runtime state, and once it did.
        assert (await client.get("/api/live")).status_code == 200
        application.state.readiness = state
        response = await client.get("/api/live")
    # Then liveness is independent of the probes.
    assert (response.status_code, response.json()) == (200, {"status": "alive"})
    assert probes.calls == 0


def lag_series(histogram: metrics.Histogram) -> dict[tuple[str, str | None], float]:
    return {(sample.name, dict(sample.labels).get("le")): sample.value for sample in histogram.collect().samples}


@pytest.mark.parametrize(("elapsed", "lag"), [(1.25, 0.25), (0.999, 0.0)])
async def test_event_loop_lag_sampler_records_how_late_each_wake_up_was(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, elapsed: float, lag: float
) -> None:
    # Given a sampler on the paced clock and a fresh lag histogram.
    histogram = metrics.Histogram("lag", "Lag.", (), (0.1, 1.0))
    monkeypatch.setattr(metrics, "EVENT_LOOP_LAG_SECONDS", histogram)
    pacing = worker_http.pacing
    worker_http.start_loop(
        "event_loop_lag",
        loop_lag.sample_event_loop_lag(1.0, clock=pacing.clock, sleep=pacing.wait),
        budget_seconds=loop_lag.PROGRESS_BUDGET_SECONDS,
    )
    assert await pacing.next_wait() == 1.0
    # When the timer resumes late, or marginally early as a timer may.
    await pacing.step(elapsed)
    # Then the lateness, never negative, is one observation, and the loop sleeps the interval again.
    series = lag_series(histogram)
    assert series[("lag_count", None)] == 1
    assert series[("lag_sum", None)] == pytest.approx(lag)
    assert series[("lag_bucket", "0.1")] == (1 if lag <= 0.1 else 0)
    assert series[("lag_bucket", "1")] == 1


async def test_event_loop_lag_sampler_keeps_readiness_green_only_while_it_beats(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP
) -> None:
    # Given a running sampler.
    monkeypatch.setattr(metrics, "EVENT_LOOP_LAG_SECONDS", metrics.Histogram("lag", "Lag.", (), (1.0,)))
    pacing = worker_http.pacing
    sampler = worker_http.start_loop(
        "event_loop_lag",
        loop_lag.sample_event_loop_lag(1.0, clock=pacing.clock, sleep=pacing.wait),
        budget_seconds=loop_lag.PROGRESS_BUDGET_SECONDS,
    )
    await pacing.paused()
    await worker_http.ready(expected=True)
    # When the loop stops waking it for longer than its progress budget.
    pacing.now += loop_lag.PROGRESS_BUDGET_SECONDS + 1
    await worker_http.ready(expected=False)
    # Then the sample that finally lands is a beat and readiness recovers.
    pacing.release()
    await pacing.paused()
    await worker_http.ready(expected=True)
    # And shutdown cancels the loop.
    await worker_http.runtime.tasks.stop()
    assert sampler.cancelled()
