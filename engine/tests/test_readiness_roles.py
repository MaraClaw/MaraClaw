import asyncio
from collections.abc import AsyncGenerator
from types import SimpleNamespace

import pytest

from app.api import background_tasks
from app.process_roles import parse_process_roles, role_enabled
from app.runtime import connectors, lifecycle, readiness
from app.runtime.tasks import OwnedTasks
from producer_admission_fixtures import producer_admission
from worker_progress_fixtures import CONNECTOR_BUDGETED_LOOPS, WORKER_BUDGETED_LOOPS

__all__ = ["producer_admission"]


@pytest.fixture(autouse=True)
async def _background_admission() -> AsyncGenerator[None]:
    background_tasks.start_background_tasks()
    try:
        yield
    finally:
        await background_tasks.drain_background_tasks(grace_seconds=0)
        background_tasks.start_background_tasks()


@pytest.mark.parametrize(
    "role", ["api", "worker", "connector", "api,connector", "worker,connector", "all", "bootstrap"]
)
@pytest.mark.parametrize(
    "failed_worker",
    [
        "trigger_daemon",
        "agent_setup",
        "agent_start_reconciler",
        "cache-invalidation-recovery",
        "auth_email",
        "broadcast_fanout",
        "openclaw_dispatch",
        "chat_history_retention",
        "operational_log_retention",
        "whatsapp_personal",
    ],
)
async def test_actual_start_registers_required_role_evidence(
    monkeypatch: pytest.MonkeyPatch, role: str, failed_worker: str
) -> None:
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
        company_transfer_cleanup,
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
    from app.services.channels import whatsapp_personal_supervisor
    from app.services.tavily import export

    runtime = lifecycle.Runtime()
    subscriber = SimpleNamespace(ready=False)
    gate = asyncio.Event()
    bootstrapped: list[bool] = []

    async def noop(*_args: object) -> None:
        return None

    async def bootstrap() -> None:
        bootstrapped.append(runtime.readiness.initialized)

    async def dependency() -> bool:
        return True

    async def loop() -> None:
        await asyncio.Event().wait()

    class Connector:
        def open(self) -> None:
            return None

        def close(self) -> None:
            return None

        async def reconcile_clients(self) -> None:
            await gate.wait()

        async def stop_all(self) -> None:
            return None

    monkeypatch.setattr(lifecycle, "init_pool", noop)
    monkeypatch.setattr(lifecycle, "get_redis", noop)
    monkeypatch.setattr(lifecycle, "bootstrap", bootstrap)
    monkeypatch.setattr(lifecycle, "configure_logging", lambda **_kwargs: None)
    monkeypatch.setattr(lifecycle, "intercept_standard_logging", lambda: None)
    monkeypatch.setattr(lifecycle, "log_bwrap_startup_status", lambda: None)
    monkeypatch.setattr(lifecycle, "shutdown_logging", lambda: None)
    monkeypatch.setattr(lifecycle.realtime_router, "start", noop)
    monkeypatch.setattr(lifecycle.realtime_router, "stop", noop)
    monkeypatch.setattr(lifecycle, "close_pool", noop)
    monkeypatch.setattr(lifecycle, "close_redis", noop)
    monkeypatch.setattr(lifecycle, "shutdown_llm_clients", noop)
    monkeypatch.setattr(audit_logger, "write_audit_log", noop)
    monkeypatch.setattr(trigger_daemon, "start_trigger_daemon", loop)
    monkeypatch.setattr(heartbeat, "start_heartbeat", loop)
    monkeypatch.setattr(agent_setup, "start_agent_setup_worker", loop)
    monkeypatch.setattr(agent_start_reconciler, "agent_start_reconciler_loop", loop)
    monkeypatch.setattr(cache_invalidation_worker, "start_cache_invalidation_worker", loop)
    monkeypatch.setattr(auth_email_worker, "start_auth_email_worker", loop)
    monkeypatch.setattr(broadcast_fanout_worker, "start_broadcast_fanout_worker", loop)
    monkeypatch.setattr(broadcast_email_worker, "start_broadcast_email_worker", loop)
    monkeypatch.setattr(export, "start_web_search_export_daemon", loop)
    monkeypatch.setattr(scheduler, "start_scheduler", loop)
    monkeypatch.setattr(gateway_report_relay, "start_gateway_report_relay", loop)
    monkeypatch.setattr(openclaw_update_worker, "start_openclaw_update_worker", loop)
    monkeypatch.setattr(organization_admission_email_worker, "start_organization_admission_email_worker", loop)
    monkeypatch.setattr(openclaw_dispatch_reconciler, "openclaw_dispatch_reconciler_loop", loop)
    monkeypatch.setattr(openclaw_delayed_replies, "openclaw_delayed_reply_loop", loop)
    monkeypatch.setattr(task_lease, "task_lease_recovery_loop", loop)
    monkeypatch.setattr(chat_history_retention_worker, "start_chat_history_retention_worker", loop)
    monkeypatch.setattr(agent_workspace_quota_worker, "start_agent_workspace_quota_worker", loop)
    monkeypatch.setattr(workspace_mirror_worker, "start_workspace_mirror_worker", loop)
    monkeypatch.setattr(agent_resource_pressure_worker, "start_agent_resource_pressure_worker", loop)
    monkeypatch.setattr(operational_log_retention_worker, "start_operational_log_retention_worker", loop)
    monkeypatch.setattr(capacity_renewal_worker, "start_capacity_renewal_worker", loop)
    monkeypatch.setattr(company_transfer_cleanup, "start_transfer_cleanup_reconciler", loop)
    monkeypatch.setattr(whatsapp_personal_supervisor, "personal_supervisor", loop)
    monkeypatch.setattr(lifecycle, "connector_managers", lambda: {"connector": Connector()})
    monkeypatch.setattr(runtime.incidents, "start", lambda: None)
    monkeypatch.setattr(runtime.incidents, "stop", noop)
    monkeypatch.setattr(runtime.proxy, "start", noop)
    monkeypatch.setattr(runtime.proxy, "stop", noop)
    monkeypatch.setattr(readiness, "ping_pool", dependency)
    monkeypatch.setattr(readiness, "ping_redis", dependency)
    monkeypatch.setattr(readiness, "realtime_router", subscriber)
    roles = parse_process_roles(role)
    try:
        await asyncio.wait_for(runtime.start(roles), 5)
        required = runtime.tasks.required
        assert ("trigger_daemon" in required) == role_enabled(roles, "worker")
        assert ("agent_setup" in required) == role_enabled(roles, "worker")
        assert ("agent_start_reconciler" in required) == role_enabled(roles, "worker")
        if role_enabled(roles, "worker"):
            assert required["agent_start_reconciler"][1] is False
        assert ("gateway_report_relay" in required) == role_enabled(roles, "worker")
        assert ("openclaw_dispatch" in required) == role_enabled(roles, "worker")
        if role_enabled(roles, "worker"):
            assert required["openclaw_dispatch"][1] is False
        assert ("chat_history_retention" in required) == role_enabled(roles, "worker")
        assert ("agent_workspace_quota" in required) == role_enabled(roles, "worker")
        assert ("workspace_mirror" in required) == role_enabled(roles, "worker")
        assert ("agent_resource_pressure" in required) == role_enabled(roles, "worker")
        assert ("operational_log_retention" in required) == role_enabled(roles, "worker")
        assert ("transfer_cleanup" in required) == role_enabled(roles, "connector")
        for worker_name in ("cache-invalidation-recovery", "auth_email", "broadcast_fanout"):
            assert (worker_name in required) == role_enabled(roles, "worker")
            if role_enabled(roles, "worker"):
                task, finite = required[worker_name]
                assert not finite
                assert not task.done()
        assert set(runtime.tasks.restarts) == (
            {"auth_email", "broadcast_email"} if role_enabled(roles, "worker") else set()
        )
        assert runtime.readiness.alive()
        budgeted: set[str] = set()
        if role_enabled(roles, "worker"):
            budgeted |= WORKER_BUDGETED_LOOPS
        if role_enabled(roles, "connector"):
            budgeted |= CONNECTOR_BUDGETED_LOOPS
        assert set(runtime.tasks.progress) == budgeted
        assert ("connector" in required) == role_enabled(roles, "connector")
        assert ("whatsapp_personal" in required) == role_enabled(roles, "connector")
        assert bootstrapped == ([False] if role_enabled(roles, "bootstrap") else [])
        if role_enabled(roles, "api") or role_enabled(roles, "connector") or role == "bootstrap":
            assert not await runtime.readiness.check()
        gate.set()
        for task, finite in tuple(required.values()):
            if finite:
                await asyncio.wait_for(task, 5)
        subscriber.ready = True
        assert await runtime.readiness.check() == (role != "bootstrap")
        if role_enabled(roles, "api"):
            subscriber.ready = False
            assert not await runtime.readiness.check()
            subscriber.ready = True
        if failed_worker in required:
            required[failed_worker][0].cancel()
            await asyncio.wait_for(asyncio.gather(required[failed_worker][0], return_exceptions=True), 5)
            assert not await runtime.readiness.check()
            # A dead loop (restarting or not) turns readiness red; liveness fails only once a restart budget is spent.
            assert runtime.readiness.alive()
    finally:
        await asyncio.wait_for(runtime.stop(), 5)
    assert runtime.readiness.draining


async def test_first_reconcile_pass_retries_in_place_then_registers_the_required_budgeted_loop() -> None:
    # Given a manager whose first pass hits a transient configuration read failure.
    tasks = OwnedTasks()
    passes: list[str] = []
    delays: list[float] = []
    loop_paced = asyncio.Event()

    class Manager:
        def open(self) -> None:
            return None

        def close(self) -> None:
            return None

        async def reconcile_clients(self) -> None:
            passes.append("pass")
            if len(passes) == 1:
                raise OSError("database briefly unavailable")

        async def stop_all(self) -> None:
            return None

    async def pace(seconds: float) -> None:
        delays.append(seconds)
        if seconds == 1000.0:
            loop_paced.set()
            await asyncio.Event().wait()

    # When the finite setup runs.
    runtime = connectors.ReconcileRuntime("wechat", Manager(), tasks, sleep=pace, interval_seconds=1000.0)
    await asyncio.wait_for(runtime.start(), 5)
    await asyncio.wait_for(loop_paced.wait(), 5)
    # Then the failure was retried in place after the base backoff, and setup returned with the loop required.
    assert passes == ["pass", "pass"]
    assert delays == [connectors.RETRY_BASE_SECONDS, 1000.0]
    assert tasks.ready()
    task, finite = tasks.required["wechat-reconcile"]
    assert not finite
    assert tasks.progress["wechat-reconcile"].budget_seconds == connectors.PROGRESS_BUDGET_SECONDS
    # And the loop is required: once it is gone, readiness fails.
    task.cancel()
    _ = await asyncio.gather(task, return_exceptions=True)
    assert not tasks.ready()
    await tasks.stop()
