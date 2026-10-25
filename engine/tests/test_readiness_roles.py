import asyncio
from types import SimpleNamespace
from typing import override

import pytest

from app.process_roles import parse_process_roles, role_enabled
from app.runtime import lifecycle, readiness
from app.runtime.connectors import WeChatRuntime
from app.runtime.tasks import OwnedTasks
from app.services.wechat_channel import WeChatPollManager
from producer_admission_fixtures import producer_admission

__all__ = ["producer_admission"]


@pytest.mark.parametrize(
    "role", ["api", "worker", "connector", "api,connector", "worker,connector", "all", "bootstrap"]
)
@pytest.mark.parametrize("failed_worker", ["trigger_daemon", "agent_setup", "agent_start_reconciler"])
async def test_actual_start_registers_required_role_evidence(
    monkeypatch: pytest.MonkeyPatch, role: str, failed_worker: str
) -> None:
    from app.services import (
        agent_setup,
        agent_start_reconciler,
        audit_logger,
        broadcast_email_worker,
        company_transfer_cleanup,
        gateway_report_relay,
        openclaw_update_worker,
        scheduler,
        trigger_daemon,
    )
    from app.services.linkup import export

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
        async def start_all(self) -> None:
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
    monkeypatch.setattr(agent_setup, "start_agent_setup_worker", loop)
    monkeypatch.setattr(agent_start_reconciler, "agent_start_reconciler_loop", loop)
    monkeypatch.setattr(broadcast_email_worker, "start_broadcast_email_worker", loop)
    monkeypatch.setattr(export, "start_web_search_export_daemon", loop)
    monkeypatch.setattr(scheduler, "start_scheduler", loop)
    monkeypatch.setattr(gateway_report_relay, "start_gateway_report_relay", loop)
    monkeypatch.setattr(openclaw_update_worker, "start_openclaw_update_worker", loop)
    monkeypatch.setattr(company_transfer_cleanup, "start_transfer_cleanup_reconciler", loop)
    monkeypatch.setattr(lifecycle, "connector_managers", lambda: (Connector(),))
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
        assert ("Connector" in required) == role_enabled(roles, "connector")
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
        if role_enabled(roles, "worker"):
            required[failed_worker][0].cancel()
            await asyncio.wait_for(asyncio.gather(required[failed_worker][0], return_exceptions=True), 5)
            assert not await runtime.readiness.check()
    finally:
        await asyncio.wait_for(runtime.stop(), 5)
    assert runtime.readiness.draining


async def test_wechat_first_reconciliation_is_finite_and_ongoing_loop_is_required() -> None:
    tasks = OwnedTasks()
    reconciled = asyncio.Event()
    loop_started = asyncio.Event()

    class Manager(WeChatPollManager):
        @override
        async def reconcile_clients(self) -> None:
            reconciled.set()

        @override
        async def start_all(self) -> None:
            loop_started.set()
            await asyncio.Event().wait()

    adapter = WeChatRuntime(Manager(), tasks)
    await adapter.start_all()
    await asyncio.wait_for(loop_started.wait(), 5)
    assert reconciled.is_set()
    assert tasks.ready()
    task, finite = tasks.required["wechat-reconcile"]
    assert not finite
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not tasks.ready()
    await tasks.stop()
