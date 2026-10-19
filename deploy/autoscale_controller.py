from pathlib import Path
from typing import Protocol

from deploy.autoscale_docker import Container, Service
from deploy.autoscale_lifecycle import ContainerControl, Retirement, topology
from deploy.autoscale_policy import POOLS, Demand, Policy, admit, decision
from deploy.autoscale_proxy import Snapshot


class Runtime(ContainerControl, Protocol):
    services: dict[str, Service]

    def validate_files(self) -> None: ...
    def memory(self, containers: tuple[Container, ...], policy: Policy) -> tuple[int, int, int | None]: ...
    def grow(self, service: str, count: int) -> None: ...


class Ingress(Protocol):
    def snapshot(self, ingress: str) -> Snapshot: ...
    def state(self, pool: str, slot: str, state: str) -> None: ...
    def release(self, pool: str, slot: str) -> None: ...


class Controller:
    def __init__(self, docker: Runtime, proxy: Ingress, policy: Policy, state_dir: Path):
        self.docker, self.proxy, self.policy = docker, proxy, policy
        self.retirement = Retirement(state_dir, proxy, docker)
        self.retirement.timeout = policy.drain_seconds
        self.retirement.minimum = policy.minimum
        self.previous: tuple[float, Snapshot] | None = None
        self.demands = {name: Demand() for name in POOLS}
        self.next_at = 0.0
        self.cursor = 0

    def invalidate(self) -> None:
        self.previous = None
        self.demands = {name: Demand() for name in POOLS}
        self.retirement.cancel()

    def tick(self, now: float) -> str:
        self.docker.validate_files()
        containers = self.docker.containers()
        ingress = self.docker.owned(containers, "ingress")
        if len(ingress) != 1 or not ingress[0].running or not ingress[0].healthy:
            raise ValueError("ingress topology unavailable")
        snapshot = self.proxy.snapshot(ingress[0].identity + ":" + ingress[0].started)
        backends = {name: snapshot.rows[(pool, "BACKEND")] for name, (pool, _) in POOLS.items()}
        previous = self.previous
        self.previous = now, snapshot
        valid = previous is not None and previous[1].generation == snapshot.generation and now > previous[0]
        if valid and previous is not None:
            valid = all(row.requests >= previous[1].rows[(POOLS[name][0], "BACKEND")].requests
                        for name, row in backends.items())
        if not valid or previous is None:
            self.demands = {name: Demand() for name in POOLS}
            self.retirement.cancel()
            return "baseline"
        for name, row in backends.items():
            replicas = len(self.docker.owned(containers, name))
            delta = row.requests - previous[1].rows[(POOLS[name][0], "BACKEND")].requests
            threshold = self.policy.engine_rps if name == "engine" else self.policy.web_rps
            self.demands[name].observe(delta / (now - previous[0]), row.sessions,
                                       row.queued, replicas, threshold)
        if self.retirement.intent is not None:
            if self.retirement.advance(snapshot, now, lambda: self.proxy.snapshot(
                    ingress[0].identity + ":" + ingress[0].started)):
                self.next_at = now + self.policy.cooldown_seconds
                return "removed"
            return "retiring"
        if now < self.next_at:
            return "cooldown"
        names = tuple(POOLS)
        for offset in range(len(names)):
            index = (self.cursor + offset) % len(names)
            name = names[index]
            members = self.docker.owned(containers, name)
            pool = POOLS[name][0]
            mapping = topology(members, snapshot, pool)
            if members and mapping is None:
                self.demands[name] = Demand()
                continue
            change = decision(self.demands[name], len(members), self.policy)
            if change > 0:
                total, stack, external = self.docker.memory(containers, self.policy)
                limit = self.docker.services[name].memory
                if limit <= 0 or not admit(total, stack + limit, external, self.policy):
                    continue
                self.docker.grow(name, len(members) + 1)
                self.next_at = now + self.policy.cooldown_seconds
            elif change < 0 and mapping is not None:
                candidate = sorted(members, key=lambda item: item.identity)[-1]
                self.retirement.begin(candidate, (pool, mapping[candidate.identity]), snapshot, now)
            else:
                continue
            self.demands = {service: Demand() for service in POOLS}
            self.cursor = (index + 1) % len(names)
            return f"{'grow' if change > 0 else 'retire'} service={name} replicas={len(members) + change}"
        return "steady"
