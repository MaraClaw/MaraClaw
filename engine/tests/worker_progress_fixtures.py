import asyncio
from collections.abc import AsyncIterator, Coroutine
from dataclasses import dataclass, field
from typing import Final, Literal

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
        "agent_workspace_quota",
        "agent_resource_pressure",
        "workspace_mirror",
        "agent_state_snapshot",
        "operational_log_retention",
        "capacity_renewal",
    }
)
# Worker loops the runtime never restarts: a restart cannot undo what their owners do at an exit (a cutover never
# cancelled, a Docker start without a total deadline), so a dead one stays down and fails liveness.
UNSUPERVISED_WORKER_LOOPS: Final = frozenset({"agent_start_reconciler", "openclaw_update"})
SUPERVISED_WORKER_LOOPS: Final = WORKER_BUDGETED_LOOPS - UNSUPERVISED_WORKER_LOOPS
CONNECTOR_BUDGETED_LOOPS: Final = frozenset({"whatsapp_personal", "zalo_personal", "transfer_cleanup"})
# The loop every serving process (api, worker or connector role) runs, whatever else its roles add.
PROCESS_BUDGETED_LOOPS: Final = frozenset({"event_loop_lag"})


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
        _ = await self.next_wait()

    async def next_wait(self) -> float:
        """Wait until a sleep parks and return the seconds it asked for."""
        return await asyncio.wait_for(self.entered.get(), 2)

    def release(self) -> None:
        """Let the parked sleep return without waiting for the next one."""
        self.go.put_nowait(None)

    async def step(self, seconds: float = 100.0) -> None:
        self.now += seconds
        self.go.put_nowait(None)
        await self.paused()


class Incarnations:
    """Scripted incarnations of a supervised loop; ``self`` is the coroutine factory.

    Each call to the factory makes a fresh coroutine that takes the next outcome: an exception is raised, ``"return"``
    ends the loop normally, ``None`` (and any call past the script) parks until cancelled and signals ``parked``.
    """

    def __init__(self, *outcomes: BaseException | Literal["return"] | None) -> None:
        self.outcomes: list[BaseException | Literal["return"] | None] = list(outcomes)
        self.coroutines: list[Coroutine[object, object, None]] = []
        self.tasks: list[asyncio.Task[object]] = []
        self.previous_finished: list[bool] = []
        self.parked: asyncio.Queue[None] = asyncio.Queue()

    def __call__(self) -> Coroutine[object, object, None]:
        self.previous_finished.append(all(task.done() for task in self.tasks))
        coroutine = self._run()
        self.coroutines.append(coroutine)
        return coroutine

    async def _run(self) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.tasks.append(task)
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome == "return":
            return
        self.parked.put_nowait(None)
        await asyncio.Event().wait()

    async def running(self) -> None:
        """Wait until an incarnation parks."""
        _ = await asyncio.wait_for(self.parked.get(), 2)


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

    async def live(self, *, expected: bool) -> None:
        response = await self.client.get("/api/live")
        assert response.status_code == (200 if expected else 503)
        assert response.json() == {"status": "alive" if expected else "failed"}

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
    runtime.tasks = OwnedTasks(pacing.clock, pacing.wait)
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
