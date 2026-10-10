"""Role-aware resource owner; cleanup survives startup and shutdown cancellation."""

import asyncio
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Protocol

import anyio
from fastapi import FastAPI

from app.config import get_settings
from app.core.events import close_redis
from app.core.logging import configure_logging, intercept_standard_logging, logger, shutdown_logging
from app.db.pool import close_pool, init_pool
from app.process_roles import parse_process_roles, role_enabled
from app.runtime.bootstrap import bootstrap
from app.runtime.diagnostics import log_bwrap_startup_status
from app.runtime.proxy import ShadowsocksProxy
from app.runtime.tasks import OwnedTasks
from app.services.llm.client_cache import shutdown_llm_clients
from app.services.realtime import realtime_router


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
        self.managers: tuple[ConnectorManager, ...] = ()
        self.proxy: ShadowsocksProxy = ShadowsocksProxy()
        self.pool_attempted: bool = False
        self.realtime_attempted: bool = False
        self.logging_attempted: bool = False

    async def start(self, roles: frozenset[str]) -> None:
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
        if role_enabled(roles, "bootstrap"):
            await bootstrap()
        if role_enabled(roles, "api"):
            from app.api.websocket import manager as ws_manager

            self.realtime_attempted = True
            try:
                await realtime_router.start(ws_manager.deliver_pubsub_message)
            except Exception as exc:
                logger.error("[startup] realtime router start failed: {}", exc)
        try:
            from app.services.audit_logger import write_audit_log

            await write_audit_log("server_startup", {"pid": os.getpid()})
        except Exception as exc:
            logger.warning("[startup] Startup audit failed: {}", exc)
        if role_enabled(roles, "worker"):
            from app.services.linkup.export import start_web_search_export_daemon
            from app.services.trigger_daemon import start_trigger_daemon

            self.tasks.start("trigger_daemon", start_trigger_daemon())
            self.tasks.start("web_search_export", start_web_search_export_daemon())
        if role_enabled(roles, "connector"):
            self.managers = connector_managers()
            self.tasks.start("ss-local-proxy", self.proxy.start())
            for manager in self.managers:
                self.tasks.start(type(manager).__name__, manager.start_all())

    @staticmethod
    async def _cleanup(name: str, close: Callable[[], Awaitable[None]]) -> None:
        try:
            await close()
        except (Exception, asyncio.CancelledError) as exc:
            logger.warning("[shutdown] Cleanup {} failed: {}", name, exc)

    async def stop(self) -> None:
        await self._cleanup("wrappers", self.tasks.stop)
        for manager in self.managers:
            await self._cleanup(type(manager).__name__, manager.stop_all)
        await self._cleanup("ss-local", self.proxy.stop)
        if self.realtime_attempted:
            await self._cleanup("realtime", realtime_router.stop)
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
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    roles = parse_process_roles(get_settings().PROCESS_ROLE)
    runtime = Runtime()
    try:
        await runtime.start(roles)
        yield
    finally:
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
