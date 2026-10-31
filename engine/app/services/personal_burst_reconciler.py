"""Durable due-burst reconciliation: the worker-owned required loop that recovers lost or missed shared-lane wakes.

PostgreSQL is the readiness authority (DB-clock ``ready_at``, actual oldest pending head, no delivered turn). The cursor
kept here only spreads scan cost: each pass reads one bounded keyset page of available due heads, so heads whose dispatch
stays blocked (for example an unavailable model) cannot hold the page and starve later lanes. When the scan reaches the
end, or the process restarts, it starts again from the oldest ``ready_at``; newly due earlier work is therefore reached
within one full cycle. A pass finishes its own database transaction before any wake. Waking uses the existing owned
``wake_shared_lane`` admission; the central claim is the single arbiter, so concurrent reconcilers only duplicate wakes.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from anyio import current_time, sleep

from app.core.logging import logger
from app.dao.agent_dao import agent_dao
from app.dao.personal_burst_dispatch_dao import DueHeadCursor, personal_burst_dispatch_dao
from app.records.agent import AgentRecord
from app.services.openclaw_routing import wake_shared_lane

MAX_INTERVAL_SECONDS: Final = 1.0
SCAN_FAILED: Final = "scan_failed"

type Wait = Callable[[float], Awaitable[None]]
type Wake = Callable[[AgentRecord, UUID], None]
type Clock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class ReconcilerConfig:
    interval_seconds: float = MAX_INTERVAL_SECONDS
    page_size: int = 50

    def __post_init__(self) -> None:
        if not 0 < self.interval_seconds <= MAX_INTERVAL_SECONDS:
            raise ValueError("personal burst reconcile interval must be in (0, 1] seconds")
        if self.page_size < 1:
            raise ValueError("personal burst reconcile page size must be positive")


@dataclass(frozen=True, slots=True)
class ReconcilerPorts:
    """Injection points: loop pacing, its clock and the owned wake admission."""

    wait: Wait = sleep
    clock: Clock = current_time
    wake: Wake = wake_shared_lane


class PersonalBurstReconciler:
    def __init__(self, config: ReconcilerConfig | None = None, ports: ReconcilerPorts | None = None) -> None:
        self._config: ReconcilerConfig = config or ReconcilerConfig()
        self._ports: ReconcilerPorts = ports or ReconcilerPorts()
        self._after: DueHeadCursor | None = None

    async def scan_once(self) -> int:
        """Wake the ready head of every lane on one page; returns the number of wakes requested."""
        page = await personal_burst_dispatch_dao.due_heads_page(
            self._config.page_size, available_only=True, after=self._after
        )
        heads = [(head.gateway_id, head.batch.scope.agent_id) for head in page.heads if head.batch is not None]
        agents = {agent.id: agent for agent in await agent_dao.get_many(list({agent_id for _, agent_id in heads}))}
        # Every transaction of this pass is over: wakes must not see (or defer to) an ambient connection.
        self._after = page.next_after
        woken = 0
        for gateway_id, agent_id in heads:
            if (agent := agents.get(agent_id)) is not None:
                self._ports.wake(agent, gateway_id)
                woken += 1
        return woken

    async def run(self) -> None:
        """Scan at most ``interval_seconds`` apart for the life of the worker; only cancellation ends it."""
        while True:
            started = self._ports.clock()
            try:
                _ = await self.scan_once()
            except Exception:
                logger.warning("[personal_burst] reconcile failed code={}", SCAN_FAILED)
            await self._ports.wait(max(0.0, self._config.interval_seconds - (self._ports.clock() - started)))


async def personal_burst_reconciler_loop() -> None:
    await PersonalBurstReconciler().run()
