"""Role-aware resource owner; cleanup survives startup and shutdown cancellation."""

import asyncio
import os
import sys
from collections.abc import AsyncGenerator, Callable, Coroutine
from contextlib import asynccontextmanager
from functools import partial

import anyio
from fastapi import FastAPI

from app.api.background_tasks import drain_background_tasks, start_background_tasks
from app.config import get_settings
from app.core.events import close_redis, get_redis
from app.core.incident_reporting import report_exception
from app.core.logging import configure_logging, intercept_standard_logging, logger, shutdown_logging
from app.db.pool import close_pool, init_pool
from app.process_roles import parse_process_roles, role_enabled
from app.runtime.bootstrap import bootstrap
from app.runtime.connectors import ConnectorManager, ReconcileRuntime
from app.runtime.diagnostics import log_bwrap_startup_status
from app.runtime.incident_alerts import IncidentAlerts
from app.runtime.proxy import ShadowsocksProxy
from app.runtime.readiness import Readiness
from app.runtime.shutdown_budget import SETTLE_FRACTION, ShutdownBudget, abandoned, joins_until
from app.runtime.tasks import OwnedTasks, RestartPolicy, loop_progress
from app.services.channels.outbound_http import shutdown_outbound_client
from app.services.docker_capacity import shutdown_docker_capacity
from app.services.heartbeat import close_heartbeat_admission, drain_heartbeat_tasks, open_heartbeat_admission
from app.services.llm.client_cache import shutdown_llm_clients
from app.services.mcp_client import shutdown_mcp_sessions
from app.services.openclaw_routing import close_wake_admission, drain_wake_tasks, open_wake_admission
from app.services.realtime import realtime_router
from app.services.scheduler_tasks import start_schedule_tasks, stop_schedule_tasks
from app.services.tavily.http_client import shutdown_tavily_client
from app.services.trigger_daemon import close_trigger_admission, drain_trigger_tasks, open_trigger_admission


def connector_managers() -> dict[str, ConnectorManager]:
    from app.services.dingtalk_stream import dingtalk_stream_manager
    from app.services.discord_gateway import discord_gateway_manager
    from app.services.feishu_ws import feishu_ws_manager
    from app.services.wechat_channel import wechat_poll_manager
    from app.services.wecom_stream import wecom_stream_manager

    return {
        "feishu": feishu_ws_manager,
        "dingtalk": dingtalk_stream_manager,
        "wecom": wecom_stream_manager,
        "wechat": wechat_poll_manager,
        "discord": discord_gateway_manager,
    }


class Runtime:
    def __init__(self) -> None:
        self.tasks: OwnedTasks = OwnedTasks()
        self.incidents: IncidentAlerts = IncidentAlerts()
        self.readiness: Readiness = Readiness(self.tasks)
        self.managers: dict[str, ConnectorManager] = {}
        self.proxy: ShadowsocksProxy = ShadowsocksProxy()
        self.pool_attempted: bool = False
        self.realtime_attempted: bool = False
        self.logging_attempted: bool = False

    async def start(self, roles: frozenset[str]) -> None:
        self.readiness.roles = roles
        start_background_tasks()
        start_schedule_tasks()
        open_trigger_admission()
        open_heartbeat_admission()
        open_wake_admission()
        settings = get_settings()
        self.logging_attempted = True
        _ = configure_logging(
            level=settings.LOG_LEVEL,
            fmt=settings.LOG_FORMAT,
            queue_size=settings.LOG_QUEUE_SIZE,
            color=settings.LOG_COLOR,
        )
        intercept_standard_logging()
        log_bwrap_startup_status()
        if "change-me" in settings.SECRET_KEY.lower() or "change-me" in settings.JWT_SECRET_KEY.lower():
            logger.warning("[startup] Default secrets configured; set unique SECRET_KEY and JWT_SECRET_KEY")
        self.pool_attempted = True
        _ = await init_pool()
        self.incidents.start()
        if role_enabled(roles, "bootstrap"):
            await bootstrap()
        if any(role_enabled(roles, role) for role in ("api", "worker", "connector")):
            _ = await get_redis()
        if role_enabled(roles, "api"):
            from app.api.websocket import manager as ws_manager

            self.realtime_attempted = True
            try:
                await realtime_router.start(ws_manager.deliver_pubsub_message, ws_manager.close_local_connections)
            except Exception as exc:
                report_exception(exc, source="runtime", operation="realtime-startup")
                logger.error("[startup] realtime router start failed: {}", exc)
        try:
            from app.services.audit_logger import write_audit_log

            await write_audit_log("server_startup", {"pid": os.getpid()})
        except Exception as exc:
            report_exception(exc, source="runtime", operation="startup-audit")
            logger.warning("[startup] Startup audit failed: {}", exc)
        if role_enabled(roles, "worker"):
            from app.services.agent_resource_pressure_worker import (
                progress_budget_seconds as agent_resource_pressure_budget_seconds,
                start_agent_resource_pressure_worker,
            )
            from app.services.agent_setup import (
                PROGRESS_BUDGET_SECONDS as AGENT_SETUP_BUDGET_SECONDS,
                start_agent_setup_worker,
            )
            from app.services.agent_start_reconciler import (
                PROGRESS_BUDGET_SECONDS as AGENT_START_BUDGET_SECONDS,
                agent_start_reconciler_loop,
            )
            from app.services.agent_workspace_quota_worker import (
                progress_budget_seconds as agent_workspace_quota_budget_seconds,
                start_agent_workspace_quota_worker,
            )
            from app.services.auth_email_worker import (
                PROGRESS_BUDGET_SECONDS as AUTH_EMAIL_BUDGET_SECONDS,
                start_auth_email_worker,
            )
            from app.services.broadcast_email_worker import (
                PROGRESS_BUDGET_SECONDS as BROADCAST_EMAIL_BUDGET_SECONDS,
                start_broadcast_email_worker,
            )
            from app.services.broadcast_fanout_worker import (
                PROGRESS_BUDGET_SECONDS as BROADCAST_FANOUT_BUDGET_SECONDS,
                start_broadcast_fanout_worker,
            )
            from app.services.cache_invalidation_worker import (
                PROGRESS_BUDGET_SECONDS as CACHE_INVALIDATION_BUDGET_SECONDS,
                start_cache_invalidation_worker,
            )
            from app.services.capacity_renewal_worker import (
                progress_budget_seconds as capacity_renewal_budget_seconds,
                start_capacity_renewal_worker,
            )
            from app.services.chat_history_retention_worker import (
                PROGRESS_BUDGET_SECONDS as CHAT_HISTORY_RETENTION_BUDGET_SECONDS,
                start_chat_history_retention_worker,
            )
            from app.services.gateway_report_relay import (
                PROGRESS_BUDGET_SECONDS as GATEWAY_REPORT_RELAY_BUDGET_SECONDS,
                start_gateway_report_relay,
            )
            from app.services.heartbeat import PROGRESS_BUDGET_SECONDS as HEARTBEAT_BUDGET_SECONDS, start_heartbeat
            from app.services.openclaw_delayed_replies import (
                PROGRESS_BUDGET_SECONDS as OPENCLAW_DELAYED_REPLIES_BUDGET_SECONDS,
                openclaw_delayed_reply_loop,
            )
            from app.services.openclaw_dispatch_reconciler import (
                PROGRESS_BUDGET_SECONDS as OPENCLAW_DISPATCH_BUDGET_SECONDS,
                openclaw_dispatch_reconciler_loop,
            )
            from app.services.openclaw_update_worker import (
                PROGRESS_BUDGET_SECONDS as OPENCLAW_UPDATE_BUDGET_SECONDS,
                start_openclaw_update_worker,
            )
            from app.services.operational_log_retention_worker import (
                PROGRESS_BUDGET_SECONDS as OPERATIONAL_LOG_RETENTION_BUDGET_SECONDS,
                start_operational_log_retention_worker,
            )
            from app.services.organization_admission_email_worker import (
                PROGRESS_BUDGET_SECONDS as ADMISSION_EMAIL_BUDGET_SECONDS,
                start_organization_admission_email_worker,
            )
            from app.services.scheduler import PROGRESS_BUDGET_SECONDS as SCHEDULER_BUDGET_SECONDS, start_scheduler
            from app.services.system_email_service import wait_for_abandoned_sends
            from app.services.task_lease import (
                PROGRESS_BUDGET_SECONDS as TASK_LEASE_RECOVERY_BUDGET_SECONDS,
                task_lease_recovery_loop,
            )
            from app.services.tavily.export import (
                PROGRESS_BUDGET_SECONDS as WEB_SEARCH_EXPORT_BUDGET_SECONDS,
                start_web_search_export_daemon,
            )
            from app.services.trigger_daemon import (
                PROGRESS_BUDGET_SECONDS as TRIGGER_DAEMON_BUDGET_SECONDS,
                start_trigger_daemon,
            )
            from app.services.workspace_mirror_worker import (
                progress_budget_seconds as workspace_mirror_budget_seconds,
                start_workspace_mirror_worker,
            )

            self.tasks.start(
                "trigger_daemon",
                start_trigger_daemon(),
                requirement="loop",
                progress_budget_seconds=TRIGGER_DAEMON_BUDGET_SECONDS,
            )
            self.tasks.start(
                "heartbeat",
                start_heartbeat(),
                requirement="loop",
                progress_budget_seconds=HEARTBEAT_BUDGET_SECONDS,
            )
            self.tasks.start(
                "agent_setup",
                start_agent_setup_worker(),
                requirement="loop",
                progress_budget_seconds=AGENT_SETUP_BUDGET_SECONDS,
            )
            self.tasks.start(
                "agent_start_reconciler",
                agent_start_reconciler_loop(),
                requirement="loop",
                progress_budget_seconds=AGENT_START_BUDGET_SECONDS,
            )
            self.tasks.start(
                "cache-invalidation-recovery",
                start_cache_invalidation_worker(),
                requirement="loop",
                progress_budget_seconds=CACHE_INVALIDATION_BUDGET_SECONDS,
            )
            mail_restarts = RestartPolicy(
                budget=settings.WORKER_LOOP_RESTART_BUDGET,
                max_backoff_seconds=settings.WORKER_LOOP_RESTART_MAX_BACKOFF_SECONDS,
                before_restart=wait_for_abandoned_sends,
            )
            self.tasks.start_supervised(
                "auth_email",
                start_auth_email_worker,
                progress_budget_seconds=AUTH_EMAIL_BUDGET_SECONDS,
                policy=mail_restarts,
            )
            self.tasks.start(
                "broadcast_fanout",
                start_broadcast_fanout_worker(),
                requirement="loop",
                progress_budget_seconds=BROADCAST_FANOUT_BUDGET_SECONDS,
            )
            self.tasks.start_supervised(
                "broadcast_email",
                start_broadcast_email_worker,
                progress_budget_seconds=BROADCAST_EMAIL_BUDGET_SECONDS,
                policy=mail_restarts,
            )
            self.tasks.start(
                "chat_history_retention",
                start_chat_history_retention_worker(),
                requirement="loop",
                progress_budget_seconds=CHAT_HISTORY_RETENTION_BUDGET_SECONDS,
            )
            self.tasks.start(
                "agent_workspace_quota",
                start_agent_workspace_quota_worker(),
                requirement="loop",
                progress_budget_seconds=agent_workspace_quota_budget_seconds(),
            )
            self.tasks.start(
                "workspace_mirror",
                start_workspace_mirror_worker(),
                requirement="loop",
                progress_budget_seconds=workspace_mirror_budget_seconds(),
            )
            self.tasks.start(
                "agent_resource_pressure",
                start_agent_resource_pressure_worker(),
                requirement="loop",
                progress_budget_seconds=agent_resource_pressure_budget_seconds(),
            )
            self.tasks.start(
                "capacity_renewal",
                start_capacity_renewal_worker(),
                requirement="loop",
                progress_budget_seconds=capacity_renewal_budget_seconds(),
            )
            self.tasks.start(
                "operational_log_retention",
                start_operational_log_retention_worker(),
                requirement="loop",
                progress_budget_seconds=OPERATIONAL_LOG_RETENTION_BUDGET_SECONDS,
            )
            self.tasks.start(
                "organization_admission_email",
                start_organization_admission_email_worker(),
                requirement="loop",
                progress_budget_seconds=ADMISSION_EMAIL_BUDGET_SECONDS,
            )
            self.tasks.start(
                "web_search_export",
                start_web_search_export_daemon(),
                requirement="loop",
                progress_budget_seconds=WEB_SEARCH_EXPORT_BUDGET_SECONDS,
            )
            self.tasks.start(
                "agent_scheduler",
                start_scheduler(),
                requirement="loop",
                progress_budget_seconds=SCHEDULER_BUDGET_SECONDS,
            )
            self.tasks.start(
                "gateway_report_relay",
                start_gateway_report_relay(),
                requirement="loop",
                progress_budget_seconds=GATEWAY_REPORT_RELAY_BUDGET_SECONDS,
            )
            self.tasks.start(
                "openclaw_update",
                start_openclaw_update_worker(),
                requirement="loop",
                progress_budget_seconds=OPENCLAW_UPDATE_BUDGET_SECONDS,
            )
            self.tasks.start(
                "openclaw_dispatch",
                openclaw_dispatch_reconciler_loop(),
                requirement="loop",
                progress_budget_seconds=OPENCLAW_DISPATCH_BUDGET_SECONDS,
            )
            self.tasks.start(
                "openclaw_delayed_replies",
                openclaw_delayed_reply_loop(),
                requirement="loop",
                progress_budget_seconds=OPENCLAW_DELAYED_REPLIES_BUDGET_SECONDS,
            )
            self.tasks.start(
                "task_lease_recovery",
                task_lease_recovery_loop(),
                requirement="loop",
                progress_budget_seconds=TASK_LEASE_RECOVERY_BUDGET_SECONDS,
            )
        if role_enabled(roles, "connector"):
            from app.services.channels.whatsapp_personal_supervisor import (
                PROGRESS_BUDGET_SECONDS as WHATSAPP_PERSONAL_BUDGET_SECONDS,
                personal_supervisor,
            )
            from app.services.channels.zalo_personal_supervisor import (
                PROGRESS_BUDGET_SECONDS as ZALO_PERSONAL_BUDGET_SECONDS,
                personal_supervisor as zalo_personal_supervisor,
            )
            from app.services.company_transfer_cleanup import (
                PROGRESS_BUDGET_SECONDS as TRANSFER_CLEANUP_BUDGET_SECONDS,
                start_transfer_cleanup_reconciler,
            )

            self.managers = connector_managers()
            for manager in self.managers.values():
                manager.open()
            self.tasks.start(
                "whatsapp_personal",
                personal_supervisor(),
                requirement="loop",
                progress_budget_seconds=WHATSAPP_PERSONAL_BUDGET_SECONDS,
            )
            self.tasks.start("ss-local-proxy", self.proxy.start(), requirement="setup")
            self.tasks.start(
                "zalo_personal",
                zalo_personal_supervisor(),
                requirement="loop",
                progress_budget_seconds=ZALO_PERSONAL_BUDGET_SECONDS,
            )
            for name, manager in self.managers.items():
                self.tasks.start(name, ReconcileRuntime(name, manager, self.tasks).start(), requirement="setup")

            async def reconcile_transfers() -> None:
                with loop_progress().exempt(restart_on_error=False):
                    await asyncio.gather(*(task for task, finite in self.tasks.required.values() if finite))
                await start_transfer_cleanup_reconciler()

            self.tasks.start(
                "transfer_cleanup",
                reconcile_transfers(),
                requirement="loop",
                progress_budget_seconds=TRANSFER_CLEANUP_BUDGET_SECONDS,
            )
        self.readiness.initialized = True

    @staticmethod
    async def _cleanup(name: str, close: Callable[[], Coroutine[object, object, None]], seconds: float) -> None:
        """Wait at most ``seconds`` for one step, whose joins stop a little earlier; never cancel an overrunning step."""
        loop = asyncio.get_running_loop()
        with joins_until(loop.time() + seconds * (1 - SETTLE_FRACTION)):
            step = asyncio.create_task(close(), name=f"shutdown: {name}")
        _ = await asyncio.wait((step,), timeout=seconds)
        if not step.done():
            abandoned(f"Cleanup {name}", (step,))
        elif step.cancelled():
            logger.warning("[shutdown] Cleanup {} failed: cancelled", name)
        elif (error := step.exception()) is not None:
            logger.warning("[shutdown] Cleanup {} failed: {}", name, error)

    def begin_drain(self) -> None:
        """Fail readiness and close producer admission synchronously, before any cleanup awaits."""
        self.readiness.draining = True
        for manager in self.managers.values():
            manager.close()
        close_trigger_admission()
        close_heartbeat_admission()
        close_wake_admission()

    async def stop(self) -> None:
        """Run every cleanup step in order within SHUTDOWN_CLEANUP_BUDGET_SECONDS; resources always get their tail."""
        self.begin_drain()
        budget = ShutdownBudget(get_settings().SHUTDOWN_CLEANUP_BUDGET_SECONDS, asyncio.get_running_loop().time)

        async def cleanup(name: str, close: Callable[[], Coroutine[object, object, None]]) -> None:
            await self._cleanup(name, close, budget.slice(name))

        async def drain(name: str, close: Callable[[float], Coroutine[object, object, None]]) -> None:
            # A drain waits half its slice for the work to finish, then cancels it and joins it until the deadline.
            seconds = budget.slice(name)
            await self._cleanup(name, partial(close, seconds / 2), seconds)

        await drain("route background tasks", drain_background_tasks)
        await cleanup("wrappers", self.tasks.stop)
        await cleanup("schedule executions", stop_schedule_tasks)
        for index, (name, manager) in enumerate(self.managers.items()):
            seconds = budget.slice("connector managers", parts=len(self.managers) - index)
            await self._cleanup(name, manager.stop_all, seconds)
        await drain("trigger invocations", drain_trigger_tasks)
        await drain("heartbeats", drain_heartbeat_tasks)
        await drain("inbox wakes", drain_wake_tasks)
        # Every owner of guest Docker calls has drained: drop queued calls without joining a hung CLI's thread.
        shutdown_docker_capacity()
        await cleanup("ss-local", self.proxy.stop)
        if self.realtime_attempted:
            await cleanup("realtime", realtime_router.stop)
        await drain("incident alerts", self.incidents.stop)
        if self.pool_attempted:
            await cleanup("database", close_pool)
            await cleanup("redis", close_redis)
            await cleanup("LLM clients", shutdown_llm_clients)
            await cleanup("outbound HTTP client", shutdown_outbound_client)
            await cleanup("Tavily HTTP client", shutdown_tavily_client)
            await cleanup("MCP sessions", shutdown_mcp_sessions)
        if self.logging_attempted:
            try:
                shutdown_logging()
            except Exception as exc:
                logger.warning("[shutdown] Logging cleanup failed: {}", exc)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    roles = parse_process_roles(get_settings().PROCESS_ROLE)
    runtime = Runtime()
    _app.state.readiness = runtime.readiness
    try:
        try:
            await runtime.start(roles)
        except Exception as error:
            report_exception(error, source="runtime", operation="startup")
            raise
        yield
    finally:
        runtime.begin_drain()
        original_error = sys.exc_info()[1]
        # AnyIO shields ASGI cancel scopes; asyncio.shield also handles direct Task.cancel().
        with anyio.CancelScope(shield=True):
            cleanup = asyncio.create_task(runtime.stop(), name="runtime-cleanup")
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            await cleanup
            if cancelled and original_error is None:
                raise asyncio.CancelledError
