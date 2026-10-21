from dataclasses import replace
from collections.abc import Callable
import json
from pathlib import Path
from typing import Protocol

from deploy.autoscale_docker import Container
from deploy.autoscale_proxy import Snapshot
from deploy.autoscale_policy import POOLS
from deploy.autoscale_state import Authority, Intent, Operation, ReplicaState, sync_directory


class ProxyControl(Protocol):
    def state(self, pool: str, slot: str, state: str) -> None: ...
    def release(self, pool: str, slot: str) -> None: ...


class ContainerControl(Protocol):
    @property
    def authority(self) -> ReplicaState: ...
    def validate_files(self) -> None: ...
    def containers(self) -> tuple[Container, ...]: ...
    def owned(self, containers: tuple[Container, ...], service: str) -> tuple[Container, ...]: ...
    def remove(self, identity: str) -> None: ...


def topology(containers: tuple[Container, ...], snapshot: Snapshot,
             pool: str, retiring: str = "") -> dict[str, str] | None:
    if not containers or any(not item.running or not item.healthy or not item.ip for item in containers):
        return None
    mapping: dict[str, str] = {}
    for item in containers:
        slots = [name for (backend, name), address in snapshot.addresses.items()
                 if backend == pool and address == item.ip]
        if len(slots) != 1:
            return None
        slot = slots[0]
        row = snapshot.rows.get((pool, slot))
        if (row is None or row.address.rsplit(":", 1)[0] != item.ip
                or row.status != ("MAINT" if item.identity == retiring else "UP")):
            return None
        mapping[item.identity] = slot
    active = {name for (backend, name), row in snapshot.rows.items()
              if backend == pool and name != "BACKEND" and row.status == "UP"}
    if active - set(mapping.values()):
        return None
    return mapping


class Retirement:
    def __init__(self, directory: Path, proxy: ProxyControl, docker: ContainerControl):
        self.path = directory / "pending.json"
        self.proxy = proxy
        self.docker = docker
        self.intent: Intent | None = None
        self.deadline = 0.0
        self.quiet = 0
        self.timeout = 120.0
        self.minimum = 1

    def recover(self) -> None:
        containers = self.docker.containers()
        state = self.docker.authority
        authority = state.reconcile({name: len(self.docker.owned(containers, name)) for name in POOLS},
            {item.identity for name in POOLS for item in self.docker.owned(containers, name)})
        if authority.operation is not None:
            victim = authority.operation.victim
            self.proxy.release(victim.pool, victim.slot)
            state.save(Authority(authority.counts))
        if self.path.exists():
            intent = Intent(**json.loads(self.path.read_text(encoding="utf-8")))
            intent.validate()
            self.proxy.release(intent.pool, intent.slot)
            self.path.unlink()
            sync_directory(self.path.parent)
        self.intent = None
        self.quiet = 0

    def begin(self, candidate: Container, slot: tuple[str, str], snapshot: Snapshot, now: float) -> None:
        pool, name = slot
        intent = Intent(candidate.identity, candidate.ip, candidate.service, pool, name, snapshot.generation)
        intent.validate()
        state = self.docker.authority
        authority = state.settled()
        containers = self.docker.containers()
        if any(len(self.docker.owned(containers, service)) != count for service, count in authority.counts.items()):
            raise ValueError("replica counts disagree with authority")
        state.save(replace(authority, operation=Operation("draining", intent)))
        self.intent = intent
        self.deadline = now + self.timeout
        self.quiet = 0
        self.proxy.state(pool, name, "maint")

    def cancel(self) -> None:
        if self.intent is not None:
            state = self.docker.authority
            authority = state.load()
            operation = authority.operation
            if operation is None or operation.phase != "draining" or operation.victim != self.intent:
                return
            state.save(replace(authority, operation=replace(operation, phase="releasing")))
            self.proxy.release(self.intent.pool, self.intent.slot)
            state.save(Authority(authority.counts))
            self.intent = None
            self.quiet = 0

    def advance(self, snapshot: Snapshot, now: float, verify: Callable[[], Snapshot]) -> bool:
        intent = self.intent
        if intent is None:
            return False
        state = self.docker.authority
        authority = state.load()
        if authority.operation != Operation("draining", intent):
            raise ValueError("unresolved retirement cannot resume deletion")
        containers = self.docker.owned(self.docker.containers(), intent.service)
        mapping = topology(containers, snapshot, intent.pool, intent.identity)
        candidate = next((item for item in containers if item.identity == intent.identity), None)
        if (now >= self.deadline or snapshot.generation != intent.generation
                or len(containers) <= self.minimum or mapping is None or candidate is None
                or candidate.ip != intent.ip or mapping.get(intent.identity) != intent.slot):
            self.cancel()
            return False
        row = snapshot.rows[(intent.pool, intent.slot)]
        self.quiet = self.quiet + 1 if row.quiet else 0
        if self.quiet < 2:
            return False
        fresh = self.docker.owned(self.docker.containers(), intent.service)
        if fresh != containers:
            self.cancel()
            return False
        confirmed = verify()
        if (confirmed.generation != intent.generation
                or any(confirmed.rows[key].requests < row.requests for key, row in snapshot.rows.items()
                       if key[1] == "BACKEND")
                or topology(fresh, confirmed, intent.pool, intent.identity) != mapping
                or not confirmed.rows[(intent.pool, intent.slot)].quiet):
            self.cancel()
            return False
        self.docker.validate_files()
        state.save(replace(authority, operation=Operation("removing", intent)))
        self.docker.remove(intent.identity)
        self.recover()
        return True
