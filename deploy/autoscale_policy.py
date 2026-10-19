from dataclasses import dataclass
from pathlib import Path
import math
import tomllib

GIB = 1024**3
POOLS = {"engine": ("engine_pool", 20.0), "web-a": ("admin_pool", 100.0),
         "web-e": ("member_pool", 100.0)}


@dataclass(frozen=True, slots=True)
class Policy:
    sample_seconds: float = 10
    high_samples: int = 3
    low_samples: int = 30
    cooldown_seconds: float = 120
    drain_seconds: float = 120
    minimum: int = 1
    maximum: int = 16
    capacity_gib: float = 0
    guest_gib: float = 2
    external_unlimited_gib: float = 0
    engine_rps: float = 20
    web_rps: float = 100

    @classmethod
    def load(cls, path: Path) -> "Policy":
        with path.open("rb") as stream:
            values = tomllib.load(stream)
        policy = cls(**values)
        numbers = tuple(values.values())
        if any(not isinstance(value, (int, float)) or isinstance(value, bool)
               or not math.isfinite(value) or value < 0 for value in numbers):
            raise ValueError("invalid policy number")
        if (not 1 <= policy.minimum <= policy.maximum <= 16
                or min(policy.sample_seconds, policy.high_samples, policy.low_samples,
                       policy.drain_seconds, policy.engine_rps, policy.web_rps) <= 0
                or any(not isinstance(value, int) for value in
                       (policy.minimum, policy.maximum, policy.high_samples, policy.low_samples))):
            raise ValueError("invalid policy bounds")
        return policy


@dataclass(slots=True)
class Demand:
    high: int = 0
    low: int = 0

    def observe(self, rps: float, sessions: int, queued: int, replicas: int,
                threshold: float) -> None:
        busy = rps > replicas * threshold or sessions > replicas * threshold or queued > 0
        quiet = rps < max(1, replicas - 1) * threshold / 2 and sessions == 0 and queued == 0
        self.high = self.high + 1 if busy else 0
        self.low = self.low + 1 if quiet else 0


def decision(demand: Demand, replicas: int, policy: Policy) -> int:
    if replicas < policy.maximum and (replicas < policy.minimum or demand.high >= policy.high_samples):
        return 1
    if replicas > policy.minimum and demand.low >= policy.low_samples:
        return -1
    return 0


def budget(total: int, policy: Policy) -> int:
    ceiling = min(total, int(policy.capacity_gib * GIB)) if policy.capacity_gib else total
    return ceiling - max(2 * GIB, math.ceil(ceiling * 0.15))


def admit(total: int, stack: int, external: int | None, policy: Policy) -> bool:
    return external is not None and stack + max(int(policy.guest_gib * GIB), external) <= budget(total, policy)


def capacity(total: int, fixed: int, per_replica: int, policy: Policy) -> int:
    return max(0, min(policy.maximum, (budget(total, policy) - fixed
                                     - int(policy.guest_gib * GIB)) // per_replica))
