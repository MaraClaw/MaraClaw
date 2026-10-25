"""Role-aware resource owner; cleanup survives startup and shutdown cancellation."""

import asyncio
import os
import sys
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Protocol

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
from app.runtime.diagnostics import log_bwrap_startup_status
from app.runtime.incident_alerts import IncidentAlerts
from app.runtime.proxy import ShadowsocksProxy
from app.runtime.readiness import Readiness
from app.runtime.tasks import OwnedTasks
from app.services.heartbeat import close_heartbeat_admission, drain_heartbeat_tasks, open_heartbeat_admission
from app.services.llm.client_cache import shutdown_llm_clients
from app.services.openclaw_routing import close_wake_admission, drain_wake_tasks, open_wake_admission
from app.services.realtime import realtime_router
from app.services.scheduler_tasks import start_schedule_tasks, stop_schedule_tasks
from app.services.trigger_daemon import close_trigger_admission, drain_trigger_tasks, open_trigger_admission


class ConnectorManager(Protocol):
    async def start_all(self) -> None: ...
    async def stop_all(self) -> None: ...


def connector_managers() -> tuple[ConnectorManager, ...]:
    from app.services.dingtalk_stream import dingtalk_stream_manager
    from app.services.discord_gateway import discord_gateway_manager
    from app.services.feishu_ws import feishu_ws_manager
    from app.services.wechat_channel import wechat_poll_manager
    from app.services.wecom_stream import wecom_stream_manager

    return (
        feishu_ws_manager,
        dingtalk_stream_manager,
        wecom_stream_manager,
        wechat_poll_manager,
        discord_gateway_manager,
    )


class Runtime:
    def __init__(self) -> None:
        self.tasks: OwnedTasks = OwnedTasks()
        self.incidents: IncidentAlerts = IncidentAlerts()
        self.readiness: Readiness = Readiness(self.tasks)
        self.managers: tuple[ConnectorManager, ...] = ()
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
                await realtime_router.start(ws_manager.deliver_pubsub_message)
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
            from app.services.agent_setup import start_agent_setup_worker
            from app.services.agent_start_reconciler import agent_start_reconciler_loop
            from app.services.broadcast_email_worker import start_broadcast_email_worker
            from app.services.gateway_report_relay import start_gateway_report_relay
            from app.services.linkup.export import start_web_search_export_daemon
            from app.services.openclaw_update_worker import start_openclaw_update_worker
            from app.services.scheduler import start_scheduler
            from app.services.trigger_daemon import start_trigger_daemon

            self.tasks.start("trigger_daemon", start_trigger_daemon(), requirement="loop")
            self.tasks.start("agent_setup", start_agent_setup_worker(), requirement="loop")
            self.tasks.start("agent_start_reconciler", agent_start_reconciler_loop(), requirement="loop")
            self.tasks.start("broadcast_email", start_broadcast_email_worker(), requirement="loop")
            self.tasks.start("web_search_export", start_web_search_export_daemon(), requirement="loop")
            self.tasks.start("agent_scheduler", start_scheduler(), requirement="loop")
            self.tasks.start("gateway_report_relay", start_gateway_report_relay(), requirement="loop")
            self.tasks.start("openclaw_update", start_openclaw_update_worker(), requirement="loop")
        if role_enabled(roles, "connector"):
            from app.runtime.connectors import WeChatRuntime
            from app.services.company_transfer_cleanup import start_transfer_cleanup_reconciler
            from app.services.wechat_channel import WeChatPollManager

            self.managers = connector_managers()
            self.tasks.start("ss-local-proxy", self.proxy.start(), requirement="setup")
            for manager in self.managers:
                initializer = WeChatRuntime(manager, self.tasks) if isinstance(manager, WeChatPollManager) else manager
                self.tasks.start(type(manager).__name__, initializer.start_all(), requirement="setup")

            async def reconcile_transfers() -> None:
                await asyncio.gather(*(task for task, finite in self.tasks.required.values() if finite))
                await start_transfer_cleanup_reconciler()

            self.tasks.start("transfer_cleanup", reconcile_transfers(), requirement="loop")
        self.readiness.initialized = True

    @staticmethod
    async def _cleanup(name: str, close: Callable[[], Awaitable[None]]) -> None:
        try:
            await close()
        except (Exception, asyncio.CancelledError) as exc:
            logger.warning("[shutdown] Cleanup {} failed: {}", name, exc)

    def begin_drain(self) -> None:
        """Fail readiness and close producer admission synchronously, before any cleanup awaits."""
        self.readiness.draining = True
        close_trigger_admission()
        close_heartbeat_admission()
        close_wake_admission()

    async def stop(self) -> None:
        self.begin_drain()
        await self._cleanup("route background tasks", drain_background_tasks)
        await self._cleanup("wrappers", self.tasks.stop)
        await self._cleanup("schedule executions", stop_schedule_tasks)
        for manager in self.managers:
            await self._cleanup(type(manager).__name__, manager.stop_all)
        await self._cleanup("trigger invocations", drain_trigger_tasks)
        await self._cleanup("heartbeats", drain_heartbeat_tasks)
        await self._cleanup("inbox wakes", drain_wake_tasks)
        await self._cleanup("ss-local", self.proxy.stop)
        if self.realtime_attempted:
            await self._cleanup("realtime", realtime_router.stop)
        await self._cleanup("incident alerts", self.incidents.stop)
        if self.pool_attempted:
            await self._cleanup("database", close_pool)
            await self._cleanup("redis", close_redis)
            await self._cleanup("LLM clients", shutdown_llm_clients)
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
