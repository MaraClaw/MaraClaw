"""Connector-owned IM streams: a finite first reconcile pass, then one required budgeted loop per manager."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Final, Protocol

from app.core.logging import logger
from app.runtime.tasks import OwnedTasks, capped_backoff, loop_progress

PROGRESS_BUDGET_SECONDS: Final = 300.0
RECONCILE_INTERVAL_SECONDS: Final = 30.0
RETRY_BASE_SECONDS: Final = 1.0
RETRY_CAP_SECONDS: Final = 30.0


class ConnectorManager(Protocol):
    def open(self) -> None: ...
    def close(self) -> None: ...
    async def reconcile_clients(self) -> None: ...
    async def stop_all(self) -> None: ...


class ReconcileRuntime:
    """Drive one manager's ``reconcile_clients``; only a pass that read the configuration counts as progress."""

    def __init__(
        self,
        name: str,
        manager: ConnectorManager,
        tasks: OwnedTasks,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        interval_seconds: float = RECONCILE_INTERVAL_SECONDS,
    ) -> None:
        self.name: str = name
        self.manager: ConnectorManager = manager
        self.tasks: OwnedTasks = tasks
        self._sleep: Callable[[float], Awaitable[None]] = sleep
        self._interval_seconds: float = interval_seconds

    async def start(self) -> None:
        """Finite setup: a transient first-pass failure retries in place, so readiness waits instead of failing."""
        await self._reconcile()
        self.tasks.start(
            f"{self.name}-reconcile",
            self._loop(),
            requirement="loop",
            progress_budget_seconds=PROGRESS_BUDGET_SECONDS,
        )

    async def _loop(self) -> None:
        progress = loop_progress()
        while True:
            await self._sleep(self._interval_seconds)
            await self._reconcile()
            progress.beat()

    async def _reconcile(self) -> None:
        """Return after one successful pass; a failed pass backs off (1 to 30 seconds) without a beat."""
        failures = 0
        while True:
            try:
                await self.manager.reconcile_clients()
                return
            except Exception as exc:
                delay = capped_backoff(failures, base_seconds=RETRY_BASE_SECONDS, cap_seconds=RETRY_CAP_SECONDS)
                failures += 1
                logger.opt(exception=exc).warning("[{}] Reconcile pass failed; retrying in {}s", self.name, delay)
                await self._sleep(delay)
