import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from fastapi import FastAPI

from app.api import background_tasks
from app.runtime import lifecycle
from app.runtime.tasks import OwnedTasks
from producer_admission_fixtures import producer_admission

__all__ = ["producer_admission"]


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
@pytest.mark.parametrize("broadcast_exit", ["shutdown", "return", "error", "cancel"])
async def test_runtime_role_matrix(monkeypatch: pytest.MonkeyPatch, role: str, broadcast_exit: str) -> None:
    # Given
    from app.process_roles import parse_process_roles, role_enabled
    from app.runtime import readiness
    from app.services import (
        agent_setup,
        agent_start_reconciler,
        audit_logger,
        broadcast_email_worker,
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

    broadcast_started = asyncio.Event()
    broadcast_release = asyncio.Event()
    broadcast_stopped = asyncio.Event()

    async def broadcast() -> None:
        broadcast_started.set()
        try:
            await broadcast_release.wait()
            if broadcast_exit == "error":
                raise RuntimeError("broadcast loop failed")
        finally:
            broadcast_stopped.set()

    start_reconciler_started = asyncio.Event()
    start_reconciler_stopped = asyncio.Event()

    async def reconcile_starts() -> None:
        start_reconciler_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            start_reconciler_stopped.set()

    async def probe() -> bool:
        return True

    async def close_pool() -> None:
        assert broadcast_stopped.is_set() == role_enabled(roles, "worker")
        assert start_reconciler_stopped.is_set() == role_enabled(roles, "worker")
        events.append("database")

    monkeypatch.setattr(lifecycle, "close_pool", close_pool)
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
    monkeypatch.setattr(agent_start_reconciler, "agent_start_reconciler_loop", reconcile_starts)
    monkeypatch.setattr(export, "start_web_search_export_daemon", loop)
    monkeypatch.setattr(scheduler, "start_scheduler", loop)
    monkeypatch.setattr(gateway_report_relay, "start_gateway_report_relay", loop)
    monkeypatch.setattr(openclaw_update_worker, "start_openclaw_update_worker", loop)
    monkeypatch.setattr(broadcast_email_worker, "start_broadcast_email_worker", broadcast)
    monkeypatch.setattr(readiness, "ping_pool", probe)
    monkeypatch.setattr(readiness, "ping_redis", probe)
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
    if role_enabled(roles, "worker"):
        start_task, start_finite = runtime.tasks.required["agent_start_reconciler"]
        await asyncio.wait_for(start_reconciler_started.wait(), timeout=1)
        assert not start_finite
        assert not start_task.done()
        await asyncio.wait_for(broadcast_started.wait(), timeout=1)
        task, finite = runtime.tasks.required["broadcast_email"]
        assert not finite
        assert not task.done()
        if not role_enabled(roles, "connector") and not role_enabled(roles, "api"):
            assert await runtime.readiness.check()
        if broadcast_exit == "cancel":
            start_task.cancel()
            await asyncio.gather(start_task, return_exceptions=True)
            assert not await runtime.readiness.check()
            assert "agent_start_reconciler" in runtime.tasks.required
        if broadcast_exit != "shutdown":
            if broadcast_exit == "cancel":
                task.cancel()
            else:
                broadcast_release.set()
            await asyncio.gather(task, return_exceptions=True)
            assert not await runtime.readiness.check()
            assert "broadcast_email" in runtime.tasks.required
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
    assert ("agent_start_reconciler" in names) == role_enabled(roles, "worker")
    assert start_reconciler_started.is_set() == role_enabled(roles, "worker")
    assert start_reconciler_stopped.is_set() == role_enabled(roles, "worker")
    assert ("broadcast_email" in names) == role_enabled(roles, "worker")
    assert broadcast_stopped.is_set() == role_enabled(roles, "worker")
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


def fake_producer_drains(monkeypatch: pytest.MonkeyPatch, events: list[str], failing: str | None = None) -> None:
    def drain(name: str) -> Callable[[], Awaitable[None]]:
        async def record() -> None:
            events.append(f"{name}-drain")
            if name == failing:
                raise RuntimeError(f"{name} drain failed")

        return record

    monkeypatch.setattr(lifecycle, "drain_trigger_tasks", drain("trigger"))
    monkeypatch.setattr(lifecycle, "drain_heartbeat_tasks", drain("heartbeat"))
    monkeypatch.setattr(lifecycle, "drain_wake_tasks", drain("wake"))


def producer_gates() -> dict[str, bool]:
    from app.services import heartbeat, openclaw_routing, trigger_daemon

    return {
        "trigger": trigger_daemon._admission.accepting,
        "heartbeat": heartbeat._admission.accepting,
        "wake": openclaw_routing._wake_admission.accepting,
    }


@pytest.mark.asyncio
async def test_producer_gates_close_before_the_first_awaited_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a runtime whose first awaited cleanup records the gate state
    events: list[str] = []
    fake_resources(monkeypatch, events)
    fake_producer_drains(monkeypatch, events)
    runtime = lifecycle.Runtime()
    seen: list[dict[str, bool]] = []

    async def first_cleanup() -> None:
        seen.append(producer_gates())

    monkeypatch.setattr(lifecycle, "drain_background_tasks", first_cleanup)
    # When
    await runtime.stop()
    # Then all three gates were already closed and readiness already failed
    assert seen == [{"trigger": False, "heartbeat": False, "wake": False}]
    assert runtime.readiness.draining


@pytest.mark.asyncio
@pytest.mark.parametrize("failing", [None, "trigger", "heartbeat", "wake"])
async def test_producer_drains_run_after_managers_and_before_realtime_and_pool(
    monkeypatch: pytest.MonkeyPatch, failing: str | None
) -> None:
    # Given owned children, a connector manager and producer drains where one may fail
    events: list[str] = []
    fake_resources(monkeypatch, events)
    fake_producer_drains(monkeypatch, events, failing)
    runtime = lifecycle.Runtime()
    runtime.pool_attempted = runtime.realtime_attempted = runtime.logging_attempted = True
    manager = ChildManager(events)
    runtime.managers = (manager,)
    runtime.tasks.start("fake-manager", manager.start_all())
    await manager.started.wait()
    # When
    await runtime.stop()
    # Then drains follow the loops and managers, stay separate, and a failure skips nothing
    assert events == [
        "wrapper-stopped",
        "manager-stop",
        "child-stopped",
        "trigger-drain",
        "heartbeat-drain",
        "wake-drain",
        "realtime",
        "database",
        "redis",
        "llm",
        "logging",
    ]


@pytest.mark.asyncio
async def test_cancelled_producer_drain_does_not_skip_later_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a heartbeat drain that is itself cancelled
    events: list[str] = []
    fake_resources(monkeypatch, events)
    fake_producer_drains(monkeypatch, events)

    async def cancelled() -> None:
        events.append("heartbeat-drain")
        raise asyncio.CancelledError

    monkeypatch.setattr(lifecycle, "drain_heartbeat_tasks", cancelled)
    runtime = lifecycle.Runtime()
    runtime.pool_attempted = runtime.realtime_attempted = True
    # When
    await runtime.stop()
    # Then
    assert events == ["trigger-drain", "heartbeat-drain", "wake-drain", "realtime", "database", "redis", "llm"]


@pytest.mark.asyncio
async def test_real_producer_work_is_cancelled_before_the_pool_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a real A2A invocation in flight and a pool close that records the order
    import uuid
    from functools import partial

    from app.services import trigger_daemon

    events: list[str] = []
    fake_resources(monkeypatch, events)
    started = asyncio.Event()

    async def invocation(*_args: object) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            events.append("invocation-cancelled")

    monkeypatch.setattr(trigger_daemon, "_invoke_agent_for_triggers", invocation)
    monkeypatch.setattr(lifecycle, "drain_trigger_tasks", partial(trigger_daemon.drain_trigger_tasks, grace_seconds=0))
    runtime = lifecycle.Runtime()
    runtime.pool_attempted = True
    await trigger_daemon.wake_agent_with_context(uuid.uuid4(), "hello", skip_dedup=True)
    await started.wait()
    # When
    await runtime.stop()
    # Then work was joined before database and redis closed, and late wakes are refused
    assert events[:3] == ["invocation-cancelled", "database", "redis"]
    await trigger_daemon.wake_agent_with_context(uuid.uuid4(), "late", skip_dedup=True)
    assert not trigger_daemon._BACKGROUND_TASKS


@pytest.mark.asyncio
async def test_repeated_lifespans_reopen_producer_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a bootstrap-only role with external dependencies faked
    from app.config import get_settings
    from app.services import audit_logger

    events: list[str] = []
    fake_resources(monkeypatch, events)
    fake_producer_drains(monkeypatch, events)

    async def noop(*_args: object) -> None:
        return None

    monkeypatch.setattr(get_settings(), "PROCESS_ROLE", "bootstrap")
    monkeypatch.setattr(lifecycle, "configure_logging", lambda **_kwargs: None)
    monkeypatch.setattr(lifecycle, "intercept_standard_logging", lambda: None)
    monkeypatch.setattr(lifecycle, "log_bwrap_startup_status", lambda: None)
    monkeypatch.setattr(lifecycle, "init_pool", noop)
    monkeypatch.setattr(lifecycle, "bootstrap", noop)
    monkeypatch.setattr(audit_logger, "write_audit_log", noop)
    # When / Then each lifespan starts open and ends closed
    for _ in range(2):
        async with lifecycle.lifespan(FastAPI()):
            assert producer_gates() == {"trigger": True, "heartbeat": True, "wake": True}
        assert producer_gates() == {"trigger": False, "heartbeat": False, "wake": False}
