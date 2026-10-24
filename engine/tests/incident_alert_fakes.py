"""Narrow Redis/delivery boundaries; the real queue, tasks and contexts run in tests."""

import sys
from dataclasses import dataclass, field
from types import ModuleType

import pytest
from redis.asyncio import Redis

from app.runtime import incident_alerts
from app.services.incident_report import IncidentReport


@dataclass
class AlertHarness:
    values: dict[str, str] = field(default_factory=dict)
    claims: list[tuple[str, str, int, bool]] = field(default_factory=list)
    releases: list[tuple[str, str]] = field(default_factory=list)
    delivered: list[IncidentReport] = field(default_factory=list)
    available: bool = True
    outcome: bool | Exception = True
    clock: float = 1000.0
    delivery: ModuleType = field(default_factory=lambda: ModuleType("app.services.incident_delivery"))

    async def set_key(self, name: str, value: str, *, nx: bool, ex: int) -> bool:
        self.claims.append((name, value, ex, nx))
        if name in self.values:
            return False
        self.values[name] = value
        return True

    async def evaluate(self, script: str, count: int, key: str, token: str) -> int:
        assert count == 1
        self.releases.append((key, token))
        # Interpret the ownership condition, including an unconditional-delete mutant.
        if "== ARGV[1]" not in script or self.values.get(key) == token:
            return int(self.values.pop(key, None) is not None)
        return 0

    async def deliver(self, report: IncidentReport) -> bool:
        self.delivered.append(report)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> AlertHarness:
    state = AlertHarness()
    redis = Redis(decode_responses=True)
    monkeypatch.setattr(redis, "set", state.set_key)
    monkeypatch.setattr(redis, "eval", state.evaluate)

    async def get_redis() -> Redis:
        if not state.available:
            raise ConnectionError("private redis address")
        return redis

    monkeypatch.setattr(incident_alerts, "get_cache_redis", get_redis)
    monkeypatch.setattr(incident_alerts, "monotonic", lambda: state.clock)
    monkeypatch.setattr(state.delivery, "deliver_incident", state.deliver, raising=False)
    monkeypatch.setitem(sys.modules, "app.services.incident_delivery", state.delivery)
    return state
