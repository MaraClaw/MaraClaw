from pathlib import Path
from dataclasses import replace
import tempfile
import unittest

from deploy.autoscale_docker import Container
from deploy.autoscale_lifecycle import Retirement, topology
from deploy.autoscale_proxy import Row, Snapshot


def fixtures() -> tuple[tuple[Container, ...], Snapshot]:
    containers = tuple(Container(letter * 64, "test", "engine", True, True, ip, 100, "start")
                       for letter, ip in (("a", "10.0.0.2"), ("b", "10.0.0.3")))
    rows = {("engine_pool", f"engine{index}"): Row("engine_pool", f"engine{index}", 0, 0, 0,
                                                   f"{item.ip}:8000", "UP", (0, 0, 0))
            for index, item in enumerate(containers, 1)}
    addresses = {key: row.address.split(":")[0] for key, row in rows.items()}
    rows[("engine_pool", "BACKEND")] = Row("engine_pool", "BACKEND", 10, 0, 0, "", "UP", ())
    return containers, Snapshot("generation", rows, addresses)


class ProxyProbe:
    def __init__(self) -> None:
        self.states: list[tuple[str, str, str]] = []

    def state(self, pool: str, slot: str, state: str) -> None:
        self.states.append((pool, slot, state))

    def release(self, pool: str, slot: str) -> None:
        self.state(pool, slot, "ready")


class DockerProbe:
    def __init__(self, containers: tuple[Container, ...]):
        self.current = containers
        self.removed: list[str] = []

    def containers(self) -> tuple[Container, ...]:
        return self.current

    def owned(self, containers: tuple[Container, ...], service: str) -> tuple[Container, ...]:
        return tuple(item for item in containers if item.service == service)

    def remove(self, identity: str) -> None:
        self.removed.append(identity)


class RetirementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.containers, self.snapshot = fixtures()
        self.proxy = ProxyProbe()
        self.docker = DockerProbe(self.containers)
        self.retirement = Retirement(Path(self.directory.name), self.proxy, self.docker)

    def maintaining(self, sessions: int = 0) -> Snapshot:
        rows = dict(self.snapshot.rows)
        rows[("engine_pool", "engine2")] = Row("engine_pool", "engine2", 0, sessions, 0,
                                               "10.0.0.3:8000", "MAINT", (0, 0, 0))
        return Snapshot("generation", rows, self.snapshot.addresses)

    def begin(self) -> None:
        self.retirement.begin(self.containers[-1], ("engine_pool", "engine2"), self.snapshot, 0)

    def test_removal_when_two_valid_quiet_observations_match_exact_identity(self) -> None:
        self.begin()
        self.retirement.advance(self.maintaining(), 1, self.maintaining)
        self.assertEqual(self.docker.removed, [])
        self.retirement.advance(self.maintaining(), 2, self.maintaining)
        self.assertEqual(self.docker.removed, ["b" * 64])
        self.assertEqual(self.proxy.states[-1], ("engine_pool", "engine2", "ready"))

    def test_stream_when_still_active_postpones_removal(self) -> None:
        self.begin()
        self.retirement.advance(self.maintaining(1), 1, self.maintaining)
        self.retirement.advance(self.maintaining(1), 2, self.maintaining)
        self.assertEqual(self.docker.removed, [])

    def test_timeout_when_stream_persists_restores_slot(self) -> None:
        self.begin()
        self.retirement.advance(self.maintaining(1), 121, self.maintaining)
        self.assertEqual(self.docker.removed, [])
        self.assertEqual(self.proxy.states[-1][-1], "ready")

    def test_recovery_when_controller_restarts_never_removes_pending_identity(self) -> None:
        self.begin()
        recovered = Retirement(Path(self.directory.name), self.proxy, self.docker)
        recovered.recover()
        self.assertEqual(self.docker.removed, [])
        self.assertEqual(self.proxy.states[-1][-1], "ready")

    def test_slot_when_ip_changes_cancels_without_removal(self) -> None:
        self.begin()
        addresses = dict(self.snapshot.addresses) | {("engine_pool", "engine2"): "10.0.0.9"}
        snapshot = Snapshot("generation", self.maintaining().rows, addresses)
        self.retirement.advance(snapshot, 1, self.maintaining)
        self.assertEqual(self.docker.removed, [])
        self.assertEqual(self.proxy.states[-1][-1], "ready")

    def test_topology_when_replica_unhealthy_is_ineligible(self) -> None:
        unhealthy = Container("c" * 64, "test", "engine", True, False, "10.0.0.3", 100, "start")
        result = topology((self.containers[0], unhealthy), self.snapshot, "engine_pool")
        self.assertIsNone(result)

    def test_revalidation_when_connection_appears_prevents_removal(self) -> None:
        self.begin()
        self.retirement.advance(self.maintaining(), 1, self.maintaining)
        self.retirement.advance(self.maintaining(), 2, lambda: self.maintaining(1))
        self.assertEqual(self.docker.removed, [])
        self.assertEqual(self.proxy.states[-1][-1], "ready")

    def test_revalidation_when_counter_resets_prevents_removal(self) -> None:
        self.begin()
        self.retirement.advance(self.maintaining(), 1, self.maintaining)
        rows = dict(self.maintaining().rows)
        rows[("engine_pool", "BACKEND")] = replace(rows[("engine_pool", "BACKEND")], requests=0)
        reset = Snapshot("generation", rows, self.snapshot.addresses)
        self.retirement.advance(self.maintaining(), 2, lambda: reset)
        self.assertEqual(self.docker.removed, [])

    def test_revalidation_when_full_identity_changes_with_same_ip_prevents_removal(self) -> None:
        self.begin()
        self.retirement.advance(self.maintaining(), 1, self.maintaining)
        self.docker.current = (self.containers[0], replace(self.containers[1], identity="c" * 64))
        self.retirement.advance(self.maintaining(), 2, self.maintaining)
        self.assertEqual(self.docker.removed, [])

    def test_quiescence_when_pooled_connection_remains_postpones_removal(self) -> None:
        self.begin()
        rows = dict(self.maintaining().rows)
        rows[("engine_pool", "engine2")] = replace(rows[("engine_pool", "engine2")], connections=(0, 1, 0))
        pooled = Snapshot("generation", rows, self.snapshot.addresses)
        self.retirement.advance(pooled, 1, lambda: pooled)
        self.retirement.advance(pooled, 2, lambda: pooled)
        self.assertEqual(self.docker.removed, [])
