import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx
import pytest

from app.runtime import lifecycle, readiness
from app.runtime.tasks import OwnedTasks


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
