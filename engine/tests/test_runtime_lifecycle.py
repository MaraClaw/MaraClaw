import asyncio
from collections.abc import Awaitable, Callable

import pytest
from fastapi import FastAPI

from app.runtime import lifecycle
from app.runtime.tasks import OwnedTasks


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
    from app.services import audit_logger, trigger_daemon
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

    monkeypatch.setattr(lifecycle, "configure_logging", lambda **_kwargs: None)
    monkeypatch.setattr(lifecycle, "intercept_standard_logging", lambda: None)
    monkeypatch.setattr(lifecycle, "log_bwrap_startup_status", lambda: None)
    monkeypatch.setattr(lifecycle, "init_pool", record_pool)
    monkeypatch.setattr(lifecycle, "bootstrap", record_bootstrap)
    monkeypatch.setattr(lifecycle.realtime_router, "start", realtime)
    monkeypatch.setattr(audit_logger, "write_audit_log", audit)
    monkeypatch.setattr(trigger_daemon, "start_trigger_daemon", loop)
    monkeypatch.setattr(export, "start_web_search_export_daemon", loop)
    manager = ChildManager(events)
    monkeypatch.setattr(lifecycle, "connector_managers", lambda: (manager,))
    monkeypatch.setattr(runtime.proxy, "start", loop)
    # When
    await runtime.start(roles)
    names = {task.get_name() for task in runtime.tasks.tasks}
    if role_enabled(roles, "connector"):
        await manager.started.wait()
    await runtime.stop()
    # Then
    expected = ["pool"]
    if role_enabled(roles, "bootstrap"):
        expected.append("bootstrap")
    if role_enabled(roles, "api"):
        expected.append("api")
    if role_enabled(roles, "connector"):
        expected.extend(["wrapper-stopped", "manager-stop", "child-stopped"])
    if role_enabled(roles, "api"):
        expected.append("realtime")
    assert events == [*expected, "database", "redis", "llm", "logging"]
    assert ("trigger_daemon" in names) == role_enabled(roles, "worker")
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
