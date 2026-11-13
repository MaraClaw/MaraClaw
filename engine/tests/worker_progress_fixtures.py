import asyncio
from collections.abc import AsyncIterator, Coroutine
from dataclasses import dataclass, field
from typing import Final

import httpx
import pytest

from app.runtime import lifecycle, readiness
from app.runtime.tasks import OwnedTasks

# Required loops that opt into a progress budget, per role (`wechat-reconcile` joins the connector set
# only when the connector manager is the WeChat one).
WORKER_BUDGETED_LOOPS: Final = frozenset(
    {
        "trigger_daemon",
        "heartbeat",
        "agent_setup",
        "agent_start_reconciler",
        "cache-invalidation-recovery",
        "auth_email",
        "broadcast_fanout",
        "broadcast_email",
        "organization_admission_email",
        "web_search_export",
        "agent_scheduler",
        "gateway_report_relay",
        "openclaw_update",
        "openclaw_dispatch",
        "openclaw_delayed_replies",
        "task_lease_recovery",
        "chat_history_retention",
        "operational_log_retention",
    }
)
CONNECTOR_BUDGETED_LOOPS: Final = frozenset({"whatsapp_personal", "transfer_cleanup"})


@dataclass(slots=True)
class Pacing:
    now: float = 0.0
    entered: asyncio.Queue[float] = field(default_factory=asyncio.Queue)
    go: asyncio.Queue[None] = field(default_factory=asyncio.Queue)

    def clock(self) -> float:
        return self.now

    async def wait(self, seconds: float) -> None:
        self.entered.put_nowait(seconds)
        await self.go.get()

    async def paused(self) -> None:
        _ = await asyncio.wait_for(self.entered.get(), 2)

    async def step(self, seconds: float = 100.0) -> None:
        self.now += seconds
        self.go.put_nowait(None)
        await self.paused()


@dataclass(frozen=True, slots=True)
class WorkerHTTP:
    runtime: lifecycle.Runtime
    client: httpx.AsyncClient
    pacing: Pacing

    def start_loop(
        self, name: str, loop: Coroutine[object, object, object], *, budget_seconds: float
    ) -> asyncio.Task[object]:
        """Run one loop coroutine as a required loop under ``budget_seconds`` on ``pacing.clock``."""
        self.runtime.tasks.start(name, loop, requirement="loop", progress_budget_seconds=budget_seconds)
        return self.runtime.tasks.required[name][0]

    async def ready(self, *, expected: bool) -> None:
        response = await self.client.get("/api/ready")
        assert response.status_code == (200 if expected else 503)
        assert response.json() == {"status": "ready" if expected else "not_ready"}
        health = await self.client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"


@pytest.fixture
async def worker_http(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[WorkerHTTP]:
    from app.db import pool
    from app.main import app

    pacing = Pacing()
    runtime = lifecycle.Runtime()
    runtime.tasks = OwnedTasks(pacing.clock)
    runtime.readiness = readiness.Readiness(runtime.tasks)
    runtime.readiness.roles = frozenset({"worker"})
    runtime.readiness.initialized = True

    async def probe() -> bool:
        return True

    monkeypatch.setattr(readiness, "ping_pool", probe)
    monkeypatch.setattr(readiness, "ping_redis", probe)
    monkeypatch.setattr(pool, "ping_pool", probe)
    monkeypatch.setattr(app.state, "readiness", runtime.readiness, raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        try:
            yield WorkerHTTP(runtime, client, pacing)
        finally:
            await runtime.tasks.stop()
    assert not runtime.tasks.tasks
