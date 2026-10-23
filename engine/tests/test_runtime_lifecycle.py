import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from fastapi import FastAPI

from app.api import background_tasks
from app.runtime import lifecycle
from app.runtime.tasks import OwnedTasks


@pytest.fixture(autouse=True)
async def _background_admission() -> AsyncIterator[None]:
    background_tasks.start_background_tasks()
    try:
        yield
    finally:
        await background_tasks.drain_background_tasks(grace_seconds=0)
        background_tasks.start_background_tasks()


class ChildManager:
    def __init__(self, events: list[str], fail: bool = False) -> None:
        self.events: list[str] = events
        self.fail: bool = fail
        self.children: OwnedTasks = OwnedTasks()
        self.started: asyncio.Event = asyncio.Event()

    async def start_all(self) -> None:
        async def child() -> None:
            self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.events.append("child-stopped")

        self.children.start("fake-child", child())
        try:
            await asyncio.Event().wait()
        finally:
            self.events.append("wrapper-stopped")

    async def stop_all(self) -> None:
        self.events.append("manager-stop")
        await self.children.stop()
        if self.fail:
            raise RuntimeError("cleanup failure")


def fake_resources(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    def close(name: str) -> Callable[[], Awaitable[None]]:
        async def record() -> None:
            events.append(name)

        return record

    monkeypatch.setattr(lifecycle, "close_pool", close("database"))
    monkeypatch.setattr(lifecycle.realtime_router, "stop", close("realtime"))
    monkeypatch.setattr(lifecycle, "close_redis", close("redis"))
    monkeypatch.setattr(lifecycle, "shutdown_llm_clients", close("llm"))
    monkeypatch.setattr(lifecycle, "shutdown_logging", lambda: events.append("logging"))


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_failure", [False, True])
async def test_owned_children_stop_before_resources_across_lifespans(
    monkeypatch: pytest.MonkeyPatch,
    cleanup_failure: bool,
) -> None:
    # Given
    events: list[str] = []
    fake_resources(monkeypatch, events)
    manager = ChildManager(events, cleanup_failure)

    async def start(runtime: lifecycle.Runtime, _roles: frozenset[str]) -> None:
        runtime.pool_attempted = runtime.realtime_attempted = runtime.logging_attempted = True
        runtime.managers = (manager,)
        runtime.tasks.start("fake-manager", manager.start_all())
        await manager.started.wait()

    monkeypatch.setattr(lifecycle.Runtime, "start", start)
    # When
    for _ in range(2):
        manager.started.clear()
        async with lifecycle.lifespan(FastAPI()):
            pass
    # Then
    assert (
        events
        == ["wrapper-stopped", "manager-stop", "child-stopped", "realtime", "database", "redis", "llm", "logging"] * 2
    )
    assert not manager.children.tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("cleanup_failure", [False, True])
async def test_startup_failure_or_cancellation_unwinds_children(
    monkeypatch: pytest.MonkeyPatch, cancel: bool, cleanup_failure: bool
) -> None:
    # Given
    events: list[str] = []
    fake_resources(monkeypatch, events)
    manager = ChildManager(events, cleanup_failure)
    startup_error = asyncio.CancelledError() if cancel else ValueError("original startup error")

    async def start(runtime: lifecycle.Runtime, _roles: frozenset[str]) -> None:
        runtime.pool_attempted = runtime.realtime_attempted = runtime.logging_attempted = True
        runtime.managers = (manager,)
        runtime.tasks.start("fake-manager", manager.start_all())
        await manager.started.wait()
        raise startup_error

    monkeypatch.setattr(lifecycle.Runtime, "start", start)
    # When / Then
    with pytest.raises(type(startup_error)) as raised:
        async with lifecycle.lifespan(FastAPI()):
            pytest.fail("startup must not yield")
    assert raised.value is startup_error
    assert str(raised.value) == ("" if cancel else "original startup error")
    assert events == [
        "wrapper-stopped",
        "manager-stop",
        "child-stopped",
        "realtime",
        "database",
        "redis",
        "llm",
        "logging",
    ]


@pytest.mark.asyncio
async def test_invalid_role_has_no_runtime_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    from app.config import get_settings
    from app.process_roles import InvalidProcessRoleError

    monkeypatch.setattr(get_settings(), "PROCESS_ROLE", "unknown")
    monkeypatch.setattr(lifecycle, "Runtime", lambda: pytest.fail("runtime allocated before role validation"))
    # When / Then
    with pytest.raises(InvalidProcessRoleError):
        async with lifecycle.lifespan(FastAPI()):
            pytest.fail("invalid role yielded")


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["api", "bootstrap", "worker", "connector", "all", "api,worker"])
async def test_runtime_role_matrix(monkeypatch: pytest.MonkeyPatch, role: str) -> None:
    # Given
    from app.process_roles import parse_process_roles, role_enabled
    from app.services import (
        agent_setup,
        audit_logger,
        gateway_report_relay,
        openclaw_update_worker,
        scheduler,
        scheduler_tasks,
        trigger_daemon,
    )
    from app.services.linkup import export

    events: list[str] = []
    roles = parse_process_roles(role)
    runtime = lifecycle.Runtime()
    fake_resources(monkeypatch, events)

    async def record_pool() -> None:
        events.append("pool")

    async def record_bootstrap() -> None:
        events.append("bootstrap")

    async def audit(*_args: object) -> None:
        return None

    async def realtime(_callback: object) -> None:
        events.append("api")

    async def loop() -> None:
        await asyncio.Event().wait()

    schedule_started = asyncio.Event()

    async def execution() -> None:
        schedule_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            events.append("schedule-stopped")

    monkeypatch.setattr(lifecycle, "configure_logging", lambda **_kwargs: None)
    monkeypatch.setattr(lifecycle, "intercept_standard_logging", lambda: None)
    monkeypatch.setattr(lifecycle, "log_bwrap_startup_status", lambda: None)
    monkeypatch.setattr(lifecycle, "init_pool", record_pool)
    monkeypatch.setattr(lifecycle, "get_redis", audit)
    monkeypatch.setattr(lifecycle, "bootstrap", record_bootstrap)
    monkeypatch.setattr(lifecycle.realtime_router, "start", realtime)
    monkeypatch.setattr(audit_logger, "write_audit_log", audit)
    monkeypatch.setattr(trigger_daemon, "start_trigger_daemon", loop)
    monkeypatch.setattr(agent_setup, "start_agent_setup_worker", loop)
    monkeypatch.setattr(export, "start_web_search_export_daemon", loop)
    monkeypatch.setattr(scheduler, "start_scheduler", loop)
    monkeypatch.setattr(gateway_report_relay, "start_gateway_report_relay", loop)
    monkeypatch.setattr(openclaw_update_worker, "start_openclaw_update_worker", loop)
    manager = ChildManager(events)
    monkeypatch.setattr(lifecycle, "connector_managers", lambda: (manager,))
    monkeypatch.setattr(runtime.proxy, "start", loop)
    await background_tasks.drain_background_tasks()
    # When
    await runtime.start(roles)
    route_started = asyncio.Event()

    async def route_work() -> None:
        route_started.set()

    background_tasks.schedule_background_task(route_work(), "runtime admission")
    await asyncio.wait_for(route_started.wait(), timeout=1)
    names = {task.get_name() for task in runtime.tasks.tasks}
    if role_enabled(roles, "connector"):
        await manager.started.wait()
    scheduler_tasks.schedule_execution(execution)
    await asyncio.wait_for(schedule_started.wait(), timeout=1)
    await runtime.stop()
    # Then
    expected = ["pool"]
    if role_enabled(roles, "bootstrap"):
        expected.append("bootstrap")
    if role_enabled(roles, "api"):
        expected.append("api")
    if role_enabled(roles, "connector"):
        expected.append("wrapper-stopped")
    expected.append("schedule-stopped")
    if role_enabled(roles, "connector"):
        expected.extend(["manager-stop", "child-stopped"])
    if role_enabled(roles, "api"):
        expected.append("realtime")
    assert events == [*expected, "database", "redis", "llm", "logging"]
    assert ("trigger_daemon" in names) == role_enabled(roles, "worker")
    assert ("agent_setup" in names) == role_enabled(roles, "worker")
    assert ("agent_scheduler" in names) == role_enabled(roles, "worker")
    assert ("gateway_report_relay" in names) == role_enabled(roles, "worker")
    assert ("openclaw_update" in names) == role_enabled(roles, "worker")
    assert ("ss-local-proxy" in names) == role_enabled(roles, "connector")
    assert ("ChildManager" in names) == role_enabled(roles, "connector")


@pytest.mark.asyncio
async def test_cancellation_of_active_lifespan_drains_children(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    events: list[str] = []
    fake_resources(monkeypatch, events)
    manager = ChildManager(events)
    yielded = asyncio.Event()

    async def start(runtime: lifecycle.Runtime, _roles: frozenset[str]) -> None:
        runtime.pool_attempted = runtime.realtime_attempted = runtime.logging_attempted = True
        runtime.managers = (manager,)
        runtime.tasks.start("fake-manager", manager.start_all())
        await manager.started.wait()

    async def run() -> None:
        async with lifecycle.lifespan(FastAPI()):
            yielded.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(lifecycle.Runtime, "start", start)
    # When
    task = asyncio.create_task(run())
    await yielded.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Then
    assert events == [
        "wrapper-stopped",
        "manager-stop",
        "child-stopped",
        "realtime",
        "database",
        "redis",
        "llm",
        "logging",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("drain_failure", [False, True])
async def test_route_drain_precedes_manager_and_resource_cleanup(monkeypatch, drain_failure: bool) -> None:
    # Given
    events: list[str] = []
    fake_resources(monkeypatch, events)
    runtime = lifecycle.Runtime()
    runtime.pool_attempted = runtime.logging_attempted = True
    manager = ChildManager(events)
    runtime.managers = (manager,)

    async def configure_client() -> None:
        await manager.start_all()

    background_tasks.schedule_background_task(configure_client(), "configure connector")
    await manager.started.wait()

    async def drain() -> None:
        await background_tasks.drain_background_tasks(grace_seconds=0)
        events.append("route-drained")
        if drain_failure:
            raise RuntimeError("drain failed")

    monkeypatch.setattr(lifecycle, "drain_background_tasks", drain)
    # When
    await runtime.stop()
    # Then
    assert events == [
        "wrapper-stopped",
        "route-drained",
        "manager-stop",
        "child-stopped",
        "database",
        "redis",
        "llm",
        "logging",
    ]


async def test_setup_shutdown_drains_write_before_database_closes(monkeypatch) -> None:
    from app.services.agent_setup_lock import drain_on_cancel

    events: list[str] = []
    fake_resources(monkeypatch, events)
    runtime = lifecycle.Runtime()
    runtime.pool_attempted = True
    started = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()

    async def write() -> None:
        started.set()
        await release.wait()
        events.append("setup-write-finished")

    async def setup() -> None:
        try:
            await drain_on_cancel(write())
        except asyncio.CancelledError:
            events.append("setup-released")
            raise

    async def drain_routes() -> None:
        cancelled.set()

    monkeypatch.setattr(lifecycle, "drain_background_tasks", drain_routes)
    runtime.tasks.start("agent_setup", setup(), requirement="loop")
    await started.wait()
    stop = asyncio.create_task(runtime.stop())
    await cancelled.wait()
    assert "database" not in events
    release.set()
    await asyncio.wait_for(stop, 2)
    assert events[:3] == ["setup-write-finished", "setup-released", "database"]
