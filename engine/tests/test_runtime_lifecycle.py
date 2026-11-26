import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from functools import partial
from typing import override

import anyio
import httpx
import pytest
from fastapi import FastAPI

from app.api import background_tasks
from app.config import get_settings
from app.runtime import lifecycle, tasks as owned_tasks
from app.runtime.tasks import OwnedTasks, RestartPolicy
from app.services import mcp_client
from app.services.mcp_client import MCPSessionPool
from producer_admission_fixtures import producer_admission
from test_mcp_recovery import TOOL_RESULT, FakeMCPServer, call
from worker_progress_fixtures import CONNECTOR_BUDGETED_LOOPS, WORKER_BUDGETED_LOOPS, Incarnations, Pacing

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

    def open(self) -> None:
        return None

    def close(self) -> None:
        return None

    async def reconcile_clients(self) -> None:
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


def fake_http_pools(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    def close(name: str) -> Callable[[], Awaitable[None]]:
        async def record() -> None:
            events.append(name)

        return record

    monkeypatch.setattr(lifecycle, "shutdown_outbound_client", close("outbound"))
    monkeypatch.setattr(lifecycle, "shutdown_tavily_client", close("tavily"))
    monkeypatch.setattr(lifecycle, "shutdown_mcp_sessions", close("mcp"))


async def test_shutdown_closes_tavily_then_mcp_after_outbound_client(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    fake_resources(monkeypatch, events)
    fake_http_pools(monkeypatch, events)
    runtime = lifecycle.Runtime()
    runtime.pool_attempted = True
    await runtime.stop()
    assert events == ["database", "redis", "llm", "outbound", "tavily", "mcp"]


def pooled_mcp(monkeypatch: pytest.MonkeyPatch, *, shutdown_timeout_seconds: float = 2.0) -> FakeMCPServer:
    server = FakeMCPServer()
    monkeypatch.setattr(mcp_client, "public_http_client", server.factory)
    monkeypatch.setattr(
        mcp_client, "mcp_session_pool", MCPSessionPool(shutdown_timeout_seconds=shutdown_timeout_seconds)
    )
    return server


async def test_shutdown_ends_each_same_loop_mcp_session_once(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: three pooled sessions whose server answers DELETE with 200, 404 and 405, plus a session left over
    # from an event loop that has since closed.
    server = pooled_mcp(monkeypatch)
    server.delete_status.update({"s2": 404, "s3": 405})
    sessions = mcp_client.mcp_session_pool
    for agent in range(1, 4):
        assert await call(sessions, agent=uuid.UUID(int=agent)) == TOOL_RESULT

    def call_in_another_loop() -> str:
        return asyncio.run(call(sessions, agent=uuid.UUID(int=4)))

    assert await asyncio.to_thread(call_in_another_loop) == TOOL_RESULT
    events: list[str] = []
    fake_resources(monkeypatch, events)
    runtime = lifecycle.Runtime()
    runtime.pool_attempted = runtime.logging_attempted = True

    # When
    await runtime.stop()

    # Then: one DELETE per same-loop session; their clients closed, the other loop's client dropped unawaited.
    assert sorted(seen.session or "" for seen in server.seen if seen.http == "DELETE") == ["s1", "s2", "s3"]
    assert [client.is_closed for client in server.clients] == [True, True, True, False]
    assert not sessions._entries
    assert events[-1] == "logging"


async def test_mcp_shutdown_is_bounded_when_the_server_never_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: a pooled session whose server accepts the DELETE and never answers it.
    server = pooled_mcp(monkeypatch, shutdown_timeout_seconds=0.05)
    assert await call(mcp_client.mcp_session_pool) == TOOL_RESULT
    entered = asyncio.Event()

    async def hang(_request: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.Event().wait()
        return httpx.Response(200)

    server.queue("DELETE", hang)

    # When: shutdown runs under a guard far above its own bound.
    async with asyncio.timeout(5):
        await mcp_client.shutdown_mcp_sessions()

    # Then: the DELETE was attempted, abandoned at the bound, and the client still closed.
    assert entered.is_set()
    assert server.clients[0].is_closed


async def test_failing_mcp_client_close_is_logged_and_cleanup_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: two pooled sessions, the first of whose clients fails to close.
    server = pooled_mcp(monkeypatch)
    sessions = mcp_client.mcp_session_pool
    for agent in (1, 2):
        assert await call(sessions, agent=uuid.UUID(int=agent)) == TOOL_RESULT
    failing, healthy = server.clients

    async def broken_close() -> None:
        raise RuntimeError("close failed")

    monkeypatch.setattr(failing, "aclose", broken_close)
    warnings: list[str] = []
    monkeypatch.setattr(mcp_client.logger, "warning", lambda message, *args: warnings.append(f"{message} {args}"))
    events: list[str] = []
    fake_resources(monkeypatch, events)
    runtime = lifecycle.Runtime()
    runtime.pool_attempted = runtime.logging_attempted = True

    # When
    await runtime.stop()

    # Then: the failure was logged, the other client still closed and later cleanup still ran.
    assert len([line for line in warnings if "close failed" in line]) == 1
    assert healthy.is_closed
    assert not sessions._entries
    assert events[-1] == "logging"


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
        runtime.managers = {"child": manager}
        runtime.tasks.start("fake-manager", manager.reconcile_clients())
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
        runtime.managers = {"child": manager}
        runtime.tasks.start("fake-manager", manager.reconcile_clients())
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
        agent_resource_pressure_worker,
        agent_setup,
        agent_start_reconciler,
        agent_workspace_quota_worker,
        audit_logger,
        auth_email_worker,
        broadcast_email_worker,
        broadcast_fanout_worker,
        cache_invalidation_worker,
        capacity_renewal_worker,
        chat_history_retention_worker,
        gateway_report_relay,
        heartbeat,
        openclaw_delayed_replies,
        openclaw_dispatch_reconciler,
        openclaw_update_worker,
        operational_log_retention_worker,
        organization_admission_email_worker,
        scheduler,
        scheduler_tasks,
        task_lease,
        trigger_daemon,
        workspace_mirror_worker,
    )
    from app.services.channels import whatsapp_personal_supervisor, zalo_personal_supervisor
    from app.services.tavily import export

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

    async def realtime(_callback: object, on_resubscribe: Callable[[], Awaitable[None]]) -> None:
        from app.api.websocket import manager as ws_manager

        assert on_resubscribe == ws_manager.close_local_connections
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
    burst_started = asyncio.Event()
    burst_stopped = asyncio.Event()
    new_workers = ("cache-invalidation-recovery", "auth_email", "broadcast_fanout")
    worker_started = {name: asyncio.Event() for name in new_workers}
    worker_stopped = {name: asyncio.Event() for name in new_workers}

    def required_worker(name: str) -> Callable[[], Awaitable[None]]:
        async def run() -> None:
            worker_started[name].set()
            try:
                await asyncio.Event().wait()
            finally:
                await anyio.lowlevel.checkpoint()
                worker_stopped[name].set()

        return run

    async def reconcile_starts() -> None:
        start_reconciler_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            start_reconciler_stopped.set()

    async def probe() -> bool:
        return True

    async def reconcile_bursts() -> None:
        burst_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            burst_stopped.set()

    async def close_pool() -> None:
        assert broadcast_stopped.is_set() == role_enabled(roles, "worker")
        assert start_reconciler_stopped.is_set() == role_enabled(roles, "worker")
        assert burst_stopped.is_set() == role_enabled(roles, "worker")
        assert all(event.is_set() == role_enabled(roles, "worker") for event in worker_stopped.values())
        events.append("database")

    async def close_redis() -> None:
        assert all(event.is_set() == role_enabled(roles, "worker") for event in worker_stopped.values())
        events.append("redis")

    monkeypatch.setattr(lifecycle, "close_pool", close_pool)
    monkeypatch.setattr(lifecycle, "close_redis", close_redis)
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
    monkeypatch.setattr(heartbeat, "start_heartbeat", loop)
    monkeypatch.setattr(agent_setup, "start_agent_setup_worker", loop)
    monkeypatch.setattr(agent_start_reconciler, "agent_start_reconciler_loop", reconcile_starts)
    monkeypatch.setattr(export, "start_web_search_export_daemon", loop)
    monkeypatch.setattr(scheduler, "start_scheduler", loop)
    monkeypatch.setattr(task_lease, "task_lease_recovery_loop", loop)
    monkeypatch.setattr(openclaw_delayed_replies, "openclaw_delayed_reply_loop", loop)
    monkeypatch.setattr(chat_history_retention_worker, "start_chat_history_retention_worker", loop)
    monkeypatch.setattr(agent_workspace_quota_worker, "start_agent_workspace_quota_worker", loop)
    monkeypatch.setattr(workspace_mirror_worker, "start_workspace_mirror_worker", loop)
    monkeypatch.setattr(agent_resource_pressure_worker, "start_agent_resource_pressure_worker", loop)
    monkeypatch.setattr(operational_log_retention_worker, "start_operational_log_retention_worker", loop)
    monkeypatch.setattr(capacity_renewal_worker, "start_capacity_renewal_worker", loop)
    monkeypatch.setattr(gateway_report_relay, "start_gateway_report_relay", loop)
    monkeypatch.setattr(openclaw_update_worker, "start_openclaw_update_worker", loop)
    monkeypatch.setattr(openclaw_dispatch_reconciler, "openclaw_dispatch_reconciler_loop", reconcile_bursts)
    monkeypatch.setattr(whatsapp_personal_supervisor, "personal_supervisor", loop)
    monkeypatch.setattr(zalo_personal_supervisor, "personal_supervisor", loop)
    monkeypatch.setattr(broadcast_email_worker, "start_broadcast_email_worker", broadcast)
    monkeypatch.setattr(auth_email_worker, "start_auth_email_worker", required_worker("auth_email"))
    monkeypatch.setattr(broadcast_fanout_worker, "start_broadcast_fanout_worker", required_worker("broadcast_fanout"))
    monkeypatch.setattr(
        cache_invalidation_worker, "start_cache_invalidation_worker", required_worker("cache-invalidation-recovery")
    )
    monkeypatch.setattr(organization_admission_email_worker, "start_organization_admission_email_worker", loop)
    monkeypatch.setattr(readiness, "ping_pool", probe)
    monkeypatch.setattr(readiness, "ping_redis", probe)
    manager = ChildManager(events)
    monkeypatch.setattr(lifecycle, "connector_managers", lambda: {"child": manager})
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
        for name in new_workers:
            await asyncio.wait_for(worker_started[name].wait(), timeout=1)
            worker_task, worker_finite = runtime.tasks.required[name]
            assert not worker_finite
            assert not worker_task.done()
        await asyncio.wait_for(burst_started.wait(), timeout=1)
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
    assert ("heartbeat" in names) == role_enabled(roles, "worker")
    if role_enabled(roles, "worker"):
        heartbeat_task, heartbeat_finite = runtime.tasks.required["heartbeat"]
        assert not heartbeat_finite
        assert heartbeat_task.done()
    assert ("agent_setup" in names) == role_enabled(roles, "worker")
    assert ("agent_start_reconciler" in names) == role_enabled(roles, "worker")
    assert start_reconciler_started.is_set() == role_enabled(roles, "worker")
    assert start_reconciler_stopped.is_set() == role_enabled(roles, "worker")
    assert ("broadcast_email" in names) == role_enabled(roles, "worker")
    for name in new_workers:
        assert (name in names) == role_enabled(roles, "worker")
        assert worker_started[name].is_set() == role_enabled(roles, "worker")
        assert worker_stopped[name].is_set() == role_enabled(roles, "worker")
        if role_enabled(roles, "worker"):
            assert runtime.tasks.required[name][0].cancelled()
    assert ("organization_admission_email" in names) == role_enabled(roles, "worker")
    assert broadcast_stopped.is_set() == role_enabled(roles, "worker")
    assert ("agent_scheduler" in names) == role_enabled(roles, "worker")
    assert ("task_lease_recovery" in names) == role_enabled(roles, "worker")
    assert ("gateway_report_relay" in names) == role_enabled(roles, "worker")
    assert ("openclaw_dispatch" in names) == role_enabled(roles, "worker")
    assert burst_stopped.is_set() == role_enabled(roles, "worker")
    assert ("openclaw_update" in names) == role_enabled(roles, "worker")
    assert ("web_search_export" in names) == role_enabled(roles, "worker")
    assert ("openclaw_delayed_replies" in names) == role_enabled(roles, "worker")
    assert ("chat_history_retention" in names) == role_enabled(roles, "worker")
    assert ("agent_workspace_quota" in names) == role_enabled(roles, "worker")
    assert ("workspace_mirror" in names) == role_enabled(roles, "worker")
    assert ("agent_resource_pressure" in names) == role_enabled(roles, "worker")
    assert ("operational_log_retention" in names) == role_enabled(roles, "worker")
    assert ("capacity_renewal" in names) == role_enabled(roles, "worker")
    assert ("whatsapp_personal" in names) == role_enabled(roles, "connector")
    assert ("zalo_personal" in names) == role_enabled(roles, "connector")
    assert ("transfer_cleanup" in names) == role_enabled(roles, "connector")
    assert ("ss-local-proxy" in names) == role_enabled(roles, "connector")
    assert ("child" in names) == role_enabled(roles, "connector")
    budgeted: set[str] = set()
    if role_enabled(roles, "worker"):
        budgeted |= WORKER_BUDGETED_LOOPS
    if role_enabled(roles, "connector"):
        budgeted |= CONNECTOR_BUDGETED_LOOPS
    assert set(runtime.tasks.progress) == budgeted


@pytest.mark.asyncio
async def test_cancellation_of_active_lifespan_drains_children(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    events: list[str] = []
    fake_resources(monkeypatch, events)
    manager = ChildManager(events)
    yielded = asyncio.Event()

    async def start(runtime: lifecycle.Runtime, _roles: frozenset[str]) -> None:
        runtime.pool_attempted = runtime.realtime_attempted = runtime.logging_attempted = True
        runtime.managers = {"child": manager}
        runtime.tasks.start("fake-manager", manager.reconcile_clients())
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
    runtime.managers = {"child": manager}

    async def configure_client() -> None:
        await manager.reconcile_clients()

    background_tasks.schedule_background_task(configure_client(), "configure connector")
    await manager.started.wait()

    async def drain(_grace_seconds: float) -> None:
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

    async def drain_routes(_grace_seconds: float) -> None:
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


async def test_shutdown_closes_the_http_pools_after_producer_drains(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    runtime = lifecycle.Runtime()
    runtime.pool_attempted = True

    async def drained(_grace_seconds: float) -> None:
        events.append("drained")

    monkeypatch.setattr(lifecycle, "drain_heartbeat_tasks", drained)
    fake_http_pools(monkeypatch, events)
    fake_resources(monkeypatch, events)
    await runtime.stop()
    assert events.index("drained") < events.index("outbound") < events.index("tavily") < events.index("mcp")


def fake_producer_drains(monkeypatch: pytest.MonkeyPatch, events: list[str], failing: str | None = None) -> None:
    def drain(name: str) -> Callable[[float], Awaitable[None]]:
        async def record(_grace_seconds: float) -> None:
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

    async def first_cleanup(_grace_seconds: float) -> None:
        seen.append(producer_gates())

    monkeypatch.setattr(lifecycle, "drain_background_tasks", first_cleanup)
    # When
    await runtime.stop()
    # Then all three gates were already closed and readiness already failed
    assert seen == [{"trigger": False, "heartbeat": False, "wake": False}]
    assert runtime.readiness.draining


@pytest.mark.asyncio
async def test_a_reconcile_pass_in_flight_at_drain_starts_no_client(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a connector pass parked while acquiring the source of a configured WeCom stream.
    from unittest.mock import AsyncMock

    from app.services import wecom_stream
    from app.services.channels import stream_source
    from stream_source_fixtures import connection_config

    manager, agent_id = wecom_stream.WeComStreamManager(), uuid.uuid4()
    config = connection_config(monkeypatch, agent_id, "wecom")
    monkeypatch.setattr(wecom_stream.channel_config_dao, "list_configured", AsyncMock(return_value=[config]))
    acquiring, release = asyncio.Event(), asyncio.Event()

    async def acquire(*args: object) -> stream_source.StreamSource:
        acquiring.set()
        await release.wait()
        return stream_source.stream_source(config)

    monkeypatch.setattr(wecom_stream, "acquire_stream_source", acquire)
    runtime = lifecycle.Runtime()
    runtime.managers = {"wecom": manager}
    manager.open()
    reconcile = asyncio.create_task(manager.reconcile_clients())
    try:
        await asyncio.wait_for(acquiring.wait(), 1)
        # When draining begins before the pass resumes.
        runtime.begin_drain()
        release.set()
        await asyncio.wait_for(reconcile, 1)
        # Then the pass finishes without opening a client.
        assert manager.tracked_agents() == set()
        assert manager.live_source(agent_id) is None
    finally:
        release.set()
        _ = await asyncio.gather(reconcile, return_exceptions=True)
        await manager.stop_all()


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
    runtime.managers = {"child": manager}
    runtime.tasks.start("fake-manager", manager.reconcile_clients())
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

    async def cancelled(_grace_seconds: float) -> None:
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

    async def drain_now(_grace_seconds: float) -> None:
        await trigger_daemon.drain_trigger_tasks(grace_seconds=0)

    monkeypatch.setattr(trigger_daemon, "_invoke_agent_for_triggers", invocation)
    monkeypatch.setattr(lifecycle, "drain_trigger_tasks", drain_now)
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


@pytest.mark.parametrize("budget_seconds", [90, 300])
async def test_worker_start_registers_progress_budgets_through_the_injected_clock(
    monkeypatch: pytest.MonkeyPatch,
    budget_seconds: int,
) -> None:
    # Given a worker runtime on a fake clock whose budgeted loops beat only when told to.
    from app.process_roles import parse_process_roles
    from app.runtime.tasks import loop_progress
    from app.services import (
        agent_resource_pressure_worker,
        agent_setup,
        agent_start_reconciler,
        agent_workspace_quota_worker,
        audit_logger,
        auth_email_worker,
        broadcast_email_worker,
        broadcast_fanout_worker,
        cache_invalidation_worker,
        capacity_renewal_worker,
        chat_history_retention_worker,
        gateway_report_relay,
        heartbeat,
        openclaw_delayed_replies,
        openclaw_dispatch_reconciler,
        openclaw_update_worker,
        operational_log_retention_worker,
        organization_admission_email_worker,
        scheduler,
        task_lease,
        trigger_daemon,
        workspace_mirror_worker,
    )
    from app.services.tavily import export

    now = 0.0
    runtime = lifecycle.Runtime()
    runtime.tasks = OwnedTasks(lambda: now)
    runtime.readiness = lifecycle.Readiness(runtime.tasks)
    fake_resources(monkeypatch, [])
    beat, all_beaten = asyncio.Event(), asyncio.Event()
    beaten: list[str] = []

    async def noop(*_args: object) -> None:
        return None

    async def probe() -> bool:
        return True

    def budgeted(name: str) -> Callable[[], Awaitable[None]]:
        async def run() -> None:
            await beat.wait()
            loop_progress().beat()
            beaten.append(name)
            if len(beaten) == len(WORKER_BUDGETED_LOOPS):
                all_beaten.set()
            await asyncio.Event().wait()

        return run

    for module, attribute, name in (
        (heartbeat, "start_heartbeat", "heartbeat"),
        (agent_setup, "start_agent_setup_worker", "agent_setup"),
        (export, "start_web_search_export_daemon", "web_search_export"),
        (gateway_report_relay, "start_gateway_report_relay", "gateway_report_relay"),
        (openclaw_delayed_replies, "openclaw_delayed_reply_loop", "openclaw_delayed_replies"),
        (task_lease, "task_lease_recovery_loop", "task_lease_recovery"),
        (chat_history_retention_worker, "start_chat_history_retention_worker", "chat_history_retention"),
        (agent_workspace_quota_worker, "start_agent_workspace_quota_worker", "agent_workspace_quota"),
        (agent_resource_pressure_worker, "start_agent_resource_pressure_worker", "agent_resource_pressure"),
        (workspace_mirror_worker, "start_workspace_mirror_worker", "workspace_mirror"),
        (operational_log_retention_worker, "start_operational_log_retention_worker", "operational_log_retention"),
        (capacity_renewal_worker, "start_capacity_renewal_worker", "capacity_renewal"),
    ):
        monkeypatch.setattr(module, attribute, budgeted(name))
    monkeypatch.setattr(trigger_daemon, "start_trigger_daemon", budgeted("trigger_daemon"))
    monkeypatch.setattr(agent_start_reconciler, "agent_start_reconciler_loop", budgeted("agent_start_reconciler"))
    monkeypatch.setattr(scheduler, "start_scheduler", budgeted("agent_scheduler"))
    monkeypatch.setattr(
        openclaw_dispatch_reconciler, "openclaw_dispatch_reconciler_loop", budgeted("openclaw_dispatch")
    )
    monkeypatch.setattr(broadcast_email_worker, "start_broadcast_email_worker", budgeted("broadcast_email"))
    monkeypatch.setattr(auth_email_worker, "start_auth_email_worker", budgeted("auth_email"))
    monkeypatch.setattr(broadcast_fanout_worker, "start_broadcast_fanout_worker", budgeted("broadcast_fanout"))
    monkeypatch.setattr(
        cache_invalidation_worker, "start_cache_invalidation_worker", budgeted("cache-invalidation-recovery")
    )
    monkeypatch.setattr(openclaw_update_worker, "start_openclaw_update_worker", budgeted("openclaw_update"))
    monkeypatch.setattr(
        organization_admission_email_worker,
        "start_organization_admission_email_worker",
        budgeted("organization_admission_email"),
    )
    monkeypatch.setattr(audit_logger, "write_audit_log", noop)
    monkeypatch.setattr(lifecycle, "configure_logging", lambda **_kwargs: None)
    monkeypatch.setattr(lifecycle, "intercept_standard_logging", lambda: None)
    monkeypatch.setattr(lifecycle, "log_bwrap_startup_status", lambda: None)
    monkeypatch.setattr(lifecycle, "init_pool", noop)
    monkeypatch.setattr(lifecycle, "get_redis", noop)
    monkeypatch.setattr(runtime.incidents, "start", lambda: None)
    monkeypatch.setattr(runtime.incidents, "stop", noop)
    from app.runtime import readiness

    monkeypatch.setattr(readiness, "ping_pool", probe)
    monkeypatch.setattr(readiness, "ping_redis", probe)
    # When the worker starts and the clock passes every budget without a beat.
    await runtime.start(parse_process_roles("worker"))
    try:
        assert set(runtime.tasks.progress) == WORKER_BUDGETED_LOOPS
        assert await runtime.readiness.check()
        now = budget_seconds
        # The workspace quota loop sleeps its whole interval between beats, so its budget is that interval plus an hour.
        assert runtime.tasks.progress["agent_workspace_quota"].budget_seconds == (
            agent_workspace_quota_worker.progress_budget_seconds()
        )
        assert runtime.tasks.progress["agent_resource_pressure"].budget_seconds == (
            agent_resource_pressure_worker.progress_budget_seconds()
        )
        assert runtime.tasks.progress["capacity_renewal"].budget_seconds == (
            capacity_renewal_worker.progress_budget_seconds()
        )
        assert runtime.tasks.progress["workspace_mirror"].budget_seconds == (
            workspace_mirror_worker.progress_budget_seconds()
        )
        interval_loops = {"agent_workspace_quota", "agent_resource_pressure", "capacity_renewal", "workspace_mirror"}
        slow = WORKER_BUDGETED_LOOPS - {"broadcast_fanout", *interval_loops}
        boundary_workers = {"broadcast_fanout"} if budget_seconds == 90 else slow
        assert all(not runtime.tasks.progress[name].stale() for name in boundary_workers)
        now += 1
        expected_stale = {"broadcast_fanout"}
        if budget_seconds == 300:
            expected_stale |= slow
        assert all(runtime.tasks.progress[name].stale() for name in expected_stale)
        if budget_seconds == 90:
            assert all(not runtime.tasks.progress[name].stale() for name in slow)
        assert not runtime.tasks.progress["agent_workspace_quota"].stale()
        assert not runtime.tasks.progress["agent_resource_pressure"].stale()
        assert not runtime.tasks.progress["capacity_renewal"].stale()
        assert runtime.tasks.progress["workspace_mirror"].stale() == (
            now > workspace_mirror_worker.progress_budget_seconds()
        )
        # Then readiness is stale with an unchanged ready body contract, and the loops keep running.
        assert not await runtime.readiness.check()
        assert all(not task.done() for task, _ in runtime.tasks.required.values())
        # And one beat per budgeted loop, through the real task context, recovers it.
        beat.set()
        await asyncio.wait_for(all_beaten.wait(), 5)
        assert await runtime.readiness.check()
    finally:
        await runtime.stop()


async def test_connector_start_registers_progress_budgets_and_only_the_setup_wait_is_exempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a connector runtime on a fake clock whose budgeted loops beat only when told to, and a WeChat
    # setup that stays unfinished so transfer cleanup is still waiting for it.
    from app.process_roles import parse_process_roles
    from app.runtime import connectors, readiness
    from app.runtime.tasks import loop_progress
    from app.services import audit_logger, company_transfer_cleanup, wechat_channel
    from app.services.channels import whatsapp_personal_supervisor, zalo_personal_supervisor

    now = 0.0
    runtime = lifecycle.Runtime()
    runtime.tasks = OwnedTasks(lambda: now)
    runtime.readiness = lifecycle.Readiness(runtime.tasks)
    fake_resources(monkeypatch, [])
    setup_entered, setup_gate, cleanup_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    beat, all_beaten = asyncio.Event(), asyncio.Event()
    beaten: list[str] = []

    async def noop(*_args: object) -> None:
        return None

    async def probe() -> bool:
        return True

    def budgeted(name: str) -> Callable[[], Awaitable[None]]:
        async def run() -> None:
            await beat.wait()
            loop_progress().beat()
            beaten.append(name)
            if len(beaten) == len(CONNECTOR_BUDGETED_LOOPS) + 1:
                all_beaten.set()
            await asyncio.Event().wait()

        return run

    async def reconcile_transfers() -> None:
        cleanup_entered.set()
        await budgeted("transfer_cleanup")()

    class Manager(wechat_channel.WeChatPollManager):
        @override
        async def reconcile_clients(self) -> None:
            setup_entered.set()
            await setup_gate.wait()

    loop_paced = False

    async def pace(_seconds: float) -> None:
        # The reconcile loop sleeps before each pass, so a second sleep means its first pass beat.
        nonlocal loop_paced
        if loop_paced:
            beaten.append("wechat-reconcile")
            if len(beaten) == len(CONNECTOR_BUDGETED_LOOPS) + 1:
                all_beaten.set()
            await asyncio.Event().wait()
        loop_paced = True
        await beat.wait()

    monkeypatch.setattr(whatsapp_personal_supervisor, "personal_supervisor", budgeted("whatsapp_personal"))
    monkeypatch.setattr(zalo_personal_supervisor, "personal_supervisor", budgeted("zalo_personal"))
    monkeypatch.setattr(company_transfer_cleanup, "start_transfer_cleanup_reconciler", reconcile_transfers)
    monkeypatch.setattr(whatsapp_personal_supervisor, "PROGRESS_BUDGET_SECONDS", 41.0)
    monkeypatch.setattr(company_transfer_cleanup, "PROGRESS_BUDGET_SECONDS", 42.0)
    monkeypatch.setattr(lifecycle, "connector_managers", lambda: {"wechat": Manager()})
    monkeypatch.setattr(lifecycle, "ReconcileRuntime", partial(connectors.ReconcileRuntime, sleep=pace))
    monkeypatch.setattr(audit_logger, "write_audit_log", noop)
    monkeypatch.setattr(lifecycle, "configure_logging", lambda **_kwargs: None)
    monkeypatch.setattr(lifecycle, "intercept_standard_logging", lambda: None)
    monkeypatch.setattr(lifecycle, "log_bwrap_startup_status", lambda: None)
    monkeypatch.setattr(lifecycle, "init_pool", noop)
    monkeypatch.setattr(lifecycle, "get_redis", noop)
    monkeypatch.setattr(runtime.incidents, "start", lambda: None)
    monkeypatch.setattr(runtime.incidents, "stop", noop)
    monkeypatch.setattr(runtime.proxy, "start", noop)
    monkeypatch.setattr(runtime.proxy, "stop", noop)
    monkeypatch.setattr(readiness, "ping_pool", probe)
    monkeypatch.setattr(readiness, "ping_redis", probe)
    progress = runtime.tasks.progress
    await runtime.start(parse_process_roles("connector"))
    try:
        # When far more than every budget passes while the setup is unfinished.
        await asyncio.wait_for(setup_entered.wait(), 5)
        now = 1000.0
        # Then the budgets are the modules' own, and only the transfer cleanup setup wait is not staleness.
        assert progress["whatsapp_personal"].budget_seconds == 41.0
        assert progress["transfer_cleanup"].budget_seconds == 42.0
        assert progress["whatsapp_personal"].stale()
        assert not progress["transfer_cleanup"].stale()
        # When the setup finishes, the cleanup loop starts and WeChat registers its ongoing loop.
        setup_gate.set()
        await asyncio.wait_for(cleanup_entered.wait(), 5)
        assert set(progress) == {*CONNECTOR_BUDGETED_LOOPS, "wechat-reconcile"}
        assert progress["wechat-reconcile"].budget_seconds == connectors.PROGRESS_BUDGET_SECONDS
        # Then the setup wait left the cleanup budget whole, and passing it makes readiness fail.
        now += 42
        assert not progress["transfer_cleanup"].stale()
        now += 1
        assert progress["transfer_cleanup"].stale()
        now = 1000.0 + connectors.PROGRESS_BUDGET_SECONDS + 1
        assert not await runtime.readiness.check()
        # And staleness neither cancels nor restarts a loop.
        assert all(
            not runtime.tasks.required[name][0].done() for name in (*CONNECTOR_BUDGETED_LOOPS, "wechat-reconcile")
        )
        # And one beat per loop, through the real task context, restores readiness.
        beat.set()
        await asyncio.wait_for(all_beaten.wait(), 5)
        assert await runtime.readiness.check()
    finally:
        await runtime.stop()


def supervised(
    pacing: Pacing,
    incarnations: Incarnations,
    *,
    budget: int = 5,
    ceiling: float = 30.0,
    before_restart: Callable[[], Awaitable[None]] | None = None,
) -> OwnedTasks:
    tasks = OwnedTasks(pacing.clock, pacing.wait)
    tasks.start_supervised(
        "mail",
        incarnations,
        progress_budget_seconds=300,
        policy=RestartPolicy(budget, ceiling, before_restart),
    )
    return tasks


def supervisor_of(tasks: OwnedTasks) -> asyncio.Task[object]:
    return next(task for task in tasks.tasks if task.get_name() == "mail-supervisor")


async def test_supervised_loop_restarts_with_fresh_coroutines_after_the_previous_task_finished() -> None:
    # Given a loop that crashes, returns and crashes again before it settles.
    pacing = Pacing()
    incarnations = Incarnations(RuntimeError("one"), RuntimeError("two"), "return", RuntimeError("three"), None)
    tasks = supervised(pacing, incarnations, ceiling=4.0)
    # When each backoff is released in turn.
    delays: list[float] = []
    for _ in range(4):
        delays.append(await pacing.next_wait())
        pacing.release()
    await incarnations.running()
    # Then the delays double up to the ceiling, every incarnation is a new coroutine started only after the
    # previous task had finished, and the required entry now points at the live incarnation.
    assert delays == [1.0, 2.0, 4.0, 4.0]
    assert incarnations.previous_finished == [True] * 5
    assert len({id(coroutine) for coroutine in incarnations.coroutines}) == 5
    assert tasks.restarts == {"mail": 4}
    assert tasks.required["mail"][0] is incarnations.tasks[-1]
    assert not tasks.exhausted
    # And stop cancels the supervisor and the current incarnation.
    supervisor = supervisor_of(tasks)
    await tasks.stop()
    assert supervisor.cancelled()
    assert incarnations.tasks[-1].cancelled()
    assert not tasks.tasks


async def test_stop_during_the_backoff_prevents_any_further_restart() -> None:
    # Given a crashed loop waiting out its backoff.
    pacing = Pacing()
    incarnations = Incarnations(RuntimeError("down"))
    tasks = supervised(pacing, incarnations)
    assert await pacing.next_wait() == 1.0
    supervisor = supervisor_of(tasks)
    # When shutdown begins before the delay ends.
    await tasks.stop()
    # Then no second incarnation is ever created and nothing stays owned.
    assert len(incarnations.coroutines) == 1
    assert supervisor.cancelled()
    assert tasks.restarts == {"mail": 0}
    assert not tasks.tasks
    assert not tasks.ready()


async def test_supervised_loop_gives_up_after_its_restart_budget_and_logs_every_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a loop that crashes on every incarnation and a budget of two restarts.
    messages: list[str] = []

    def record(message: str, *args: object) -> None:
        messages.append(message.format(*args))

    monkeypatch.setattr(owned_tasks.logger, "warning", record)
    monkeypatch.setattr(owned_tasks.logger, "error", record)
    pacing = Pacing()
    incarnations = Incarnations(RuntimeError("first"), RuntimeError("second"), RuntimeError("third"))
    tasks = supervised(pacing, incarnations, budget=2)
    supervisor = supervisor_of(tasks)
    for _ in range(2):
        _ = await pacing.next_wait()
        pacing.release()
    _ = await asyncio.wait_for(supervisor, 2)
    # Then the third exit is final: exhausted, never ready, no fourth incarnation.
    assert tasks.exhausted == {"mail"}
    assert tasks.restarts == {"mail": 2}
    assert len(incarnations.coroutines) == 3
    assert not tasks.ready()
    # And each restart and the give-up named the loop and the error that ended the incarnation.
    restarting = [message for message in messages if "Restarting" in message]
    assert len(restarting) == 2
    assert all(
        "mail" in message and error in message for message, error in zip(restarting, ("first", "second"), strict=True)
    )
    assert [message for message in messages if "exhausted" in message and "mail" in message and "third" in message]
    await tasks.stop()


async def test_loops_that_do_not_opt_in_are_never_restarted() -> None:
    # Given a plain required loop that crashes.
    incarnations = Incarnations(RuntimeError("crash"))
    tasks = OwnedTasks()
    tasks.start("plain", incarnations(), requirement="loop", progress_budget_seconds=300)
    task, _ = tasks.required["plain"]
    # When it ends.
    _ = await asyncio.gather(task, return_exceptions=True)
    # Then it stays dead: no supervisor, no restart state, readiness red.
    assert tasks.tasks == set()
    assert tasks.restarts == {}
    assert tasks.exhausted == set()
    assert len(incarnations.coroutines) == 1
    assert not tasks.ready()
    await tasks.stop()


async def test_before_restart_settles_leftovers_between_the_backoff_and_the_new_incarnation() -> None:
    # Given a hook standing in for work the dead incarnation left behind.
    pacing = Pacing()
    entered, leftovers_done = asyncio.Event(), asyncio.Event()

    async def settle() -> None:
        entered.set()
        await leftovers_done.wait()

    incarnations = Incarnations(RuntimeError("crash"), None)
    tasks = supervised(pacing, incarnations, before_restart=settle)
    # When the backoff ends.
    _ = await pacing.next_wait()
    pacing.release()
    await asyncio.wait_for(entered.wait(), 2)
    # Then no new incarnation starts while the leftovers are unsettled.
    assert len(incarnations.coroutines) == 1
    assert tasks.restarts == {"mail": 0}
    leftovers_done.set()
    await incarnations.running()
    assert len(incarnations.coroutines) == 2
    assert tasks.restarts == {"mail": 1}
    await tasks.stop()


async def test_a_failing_restart_hook_leaves_the_loop_down_and_exhausted() -> None:
    # Given a hook that raises.
    pacing = Pacing()

    async def broken() -> None:
        raise RuntimeError("cannot settle")

    incarnations = Incarnations(RuntimeError("crash"))
    tasks = supervised(pacing, incarnations, before_restart=broken)
    supervisor = supervisor_of(tasks)
    # When the backoff ends.
    _ = await pacing.next_wait()
    pacing.release()
    _ = await asyncio.gather(supervisor, return_exceptions=True)
    # Then the loop is reported down for good instead of silently staying dead.
    assert tasks.exhausted == {"mail"}
    assert len(incarnations.coroutines) == 1
    assert not tasks.ready()
    await tasks.stop()


async def test_worker_start_supervises_only_the_mail_loops_with_the_configured_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a worker runtime whose restart settings are one budget of three and a 1.5 s ceiling, and whose
    # auth mail loop crashes on every incarnation.
    from app.process_roles import parse_process_roles
    from app.services import (
        agent_resource_pressure_worker,
        agent_setup,
        agent_start_reconciler,
        agent_workspace_quota_worker,
        audit_logger,
        auth_email_worker,
        broadcast_email_worker,
        broadcast_fanout_worker,
        cache_invalidation_worker,
        capacity_renewal_worker,
        chat_history_retention_worker,
        gateway_report_relay,
        heartbeat,
        openclaw_delayed_replies,
        openclaw_dispatch_reconciler,
        openclaw_update_worker,
        operational_log_retention_worker,
        organization_admission_email_worker,
        scheduler,
        task_lease,
        trigger_daemon,
        workspace_mirror_worker,
    )
    from app.services.tavily import export

    pacing = Pacing()
    runtime = lifecycle.Runtime()
    runtime.tasks = OwnedTasks(pacing.clock, pacing.wait)
    runtime.readiness = lifecycle.Readiness(runtime.tasks)
    fake_resources(monkeypatch, [])
    settings = get_settings().model_copy(
        update={"WORKER_LOOP_RESTART_BUDGET": 3, "WORKER_LOOP_RESTART_MAX_BACKOFF_SECONDS": 1.5}
    )
    monkeypatch.setattr(lifecycle, "get_settings", lambda: settings)
    auth_crashes = Incarnations(*(RuntimeError("auth crash") for _ in range(4)))

    async def noop(*_args: object) -> None:
        return None

    async def loop() -> None:
        await asyncio.Event().wait()

    for module, attribute in (
        (agent_resource_pressure_worker, "start_agent_resource_pressure_worker"),
        (agent_setup, "start_agent_setup_worker"),
        (agent_start_reconciler, "agent_start_reconciler_loop"),
        (agent_workspace_quota_worker, "start_agent_workspace_quota_worker"),
        (broadcast_email_worker, "start_broadcast_email_worker"),
        (broadcast_fanout_worker, "start_broadcast_fanout_worker"),
        (cache_invalidation_worker, "start_cache_invalidation_worker"),
        (capacity_renewal_worker, "start_capacity_renewal_worker"),
        (chat_history_retention_worker, "start_chat_history_retention_worker"),
        (export, "start_web_search_export_daemon"),
        (gateway_report_relay, "start_gateway_report_relay"),
        (heartbeat, "start_heartbeat"),
        (openclaw_delayed_replies, "openclaw_delayed_reply_loop"),
        (openclaw_dispatch_reconciler, "openclaw_dispatch_reconciler_loop"),
        (openclaw_update_worker, "start_openclaw_update_worker"),
        (operational_log_retention_worker, "start_operational_log_retention_worker"),
        (organization_admission_email_worker, "start_organization_admission_email_worker"),
        (scheduler, "start_scheduler"),
        (task_lease, "task_lease_recovery_loop"),
        (trigger_daemon, "start_trigger_daemon"),
        (workspace_mirror_worker, "start_workspace_mirror_worker"),
    ):
        monkeypatch.setattr(module, attribute, loop)
    monkeypatch.setattr(auth_email_worker, "start_auth_email_worker", auth_crashes)
    monkeypatch.setattr(audit_logger, "write_audit_log", noop)
    monkeypatch.setattr(lifecycle, "configure_logging", lambda **_kwargs: None)
    monkeypatch.setattr(lifecycle, "intercept_standard_logging", lambda: None)
    monkeypatch.setattr(lifecycle, "log_bwrap_startup_status", lambda: None)
    monkeypatch.setattr(lifecycle, "init_pool", noop)
    monkeypatch.setattr(lifecycle, "get_redis", noop)
    monkeypatch.setattr(runtime.incidents, "start", lambda: None)
    monkeypatch.setattr(runtime.incidents, "stop", noop)
    # When the runtime starts and the auth loop keeps crashing through its budget.
    await runtime.start(parse_process_roles("worker"))
    try:
        assert set(runtime.tasks.restarts) == {"auth_email", "broadcast_email"}
        supervisor = next(task for task in runtime.tasks.tasks if task.get_name() == "auth_email-supervisor")
        delays: list[float] = []
        for _ in range(3):
            delays.append(await pacing.next_wait())
            pacing.release()
        _ = await asyncio.wait_for(supervisor, 2)
        # Then the settings drove the budget and the ceiling, and only the auth loop is down for good.
        assert delays == [1.0, 1.5, 1.5]
        assert runtime.tasks.exhausted == {"auth_email"}
        assert runtime.tasks.restarts == {"auth_email": 3, "broadcast_email": 0}
        assert not runtime.readiness.alive()
    finally:
        await runtime.stop()
