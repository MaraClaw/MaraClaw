import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.core import events
from app.process_roles import parse_process_roles
from app.runtime import lifecycle, readiness
from app.runtime.tasks import OwnedTasks


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
