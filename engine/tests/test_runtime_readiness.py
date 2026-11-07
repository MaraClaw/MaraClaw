import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.core import events
from app.process_roles import parse_process_roles
from app.runtime import lifecycle, readiness
from app.runtime.tasks import OwnedTasks, loop_progress
from producer_admission_fixtures import producer_admission

__all__ = ["producer_admission"]


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
