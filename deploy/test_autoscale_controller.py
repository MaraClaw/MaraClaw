from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from deploy.autoscale_controller import Controller
from deploy.autoscale_docker import Container, Service
from deploy.autoscale_policy import GIB, POOLS, Policy
from deploy.autoscale_proxy import Row, Snapshot
from deploy.autoscale_state import Authority, Binding, ReplicaState


class RuntimeProbe:
    def __init__(self, directory: Path = Path("/unused")) -> None:
        self.services = {name: Service(128 * 1024**2, 2) for name in POOLS}
        self.current = (Container("f" * 64, "test", "ingress", True, True, "10.0.0.9", 100, "start"),)
        for index, service in enumerate(POOLS, 1):
            self.current += tuple(Container(f"{index * 16 + number:064x}", "test", service, True, True,
                                            f"10.0.{index}.{number}", 100, "start") for number in (1, 2))
        self.grown: list[tuple[str, int]] = []
        self.removed: list[str] = []
        self.authority = ReplicaState(Binding("/repo", "test", str(directory), "unix:///fixture/docker.sock",
                                             "/input.env", ("/compose.yaml",)))

    def validate_files(self) -> None:
        pass

    def containers(self) -> tuple[Container, ...]:
        return self.current

    def owned(self, containers: tuple[Container, ...], service: str) -> tuple[Container, ...]:
        return tuple(item for item in containers if item.service == service)

    def memory(self, containers: tuple[Container, ...], policy: Policy) -> tuple[int, int, int | None]:
        return 32 * GIB, GIB, 0

    def grow(self, service: str, count: int) -> None:
        self.grown.append((service, count))
        template = self.owned(self.current, service)[0]
        self.current += (replace(template, identity=f"{len(self.current):064x}", ip=f"10.1.0.{len(self.current)}"),)

    def remove(self, identity: str) -> None:
        self.removed.append(identity)
        self.current = tuple(item for item in self.current if item.identity != identity)


class IngressProbe:
    def __init__(self, runtime: RuntimeProbe):
        self.generation = "one"
        self.requests = 0
        self.sessions = 0
        self.missing = False
        self.states: list[tuple[str, str, str]] = []
        self.runtime = runtime

    def snapshot(self, ingress: str) -> Snapshot:
        rows: dict[tuple[str, str], Row] = {}
        addresses: dict[tuple[str, str], str] = {}
        for service, (pool, _) in POOLS.items():
            if self.missing:
                continue
            rows[(pool, "BACKEND")] = Row(pool, "BACKEND", self.requests, self.sessions, 0, "", "UP", ())
            for index, item in enumerate(self.runtime.owned(self.runtime.current, service), 1):
                name = f"{pool.removesuffix('_pool')}{index}"
                status = "MAINT" if (pool, name, "maint") in self.states else "UP"
                rows[(pool, name)] = Row(pool, name, 0, self.sessions, 0, item.ip + ":8000", status, (0, 0, 0))
                addresses[(pool, name)] = item.ip
        return Snapshot(self.generation, rows, addresses)

    def state(self, pool: str, slot: str, state: str) -> None:
        self.states.append((pool, slot, state))

    def release(self, pool: str, slot: str) -> None:
        self.state(pool, slot, "ready")


class ControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.runtime = RuntimeProbe(Path(self.directory.name))
        self.runtime.authority.save(Authority({name: 2 for name in POOLS}))
        self.ingress = IngressProbe(self.runtime)
        self.controller = Controller(self.runtime, self.ingress,
                                     Policy(low_samples=2, high_samples=2, cooldown_seconds=0),
                                     Path(self.directory.name))
        self.controller.tick(0)

    def test_missing_stats_when_retiring_cannot_remove(self) -> None:
        self.controller.tick(1)
        self.controller.tick(2)
        self.ingress.missing = True
        with self.assertRaises(KeyError):
            self.controller.tick(3)
        self.controller.invalidate()
        self.assertEqual(self.runtime.removed, [])
        self.assertIsNone(self.controller.retirement.intent)

    def test_reset_when_request_counter_decreases_rebaselines(self) -> None:
        self.ingress.requests = 10
        self.controller.tick(1)
        self.ingress.requests = 0
        self.assertEqual(self.controller.tick(2), "baseline")
        self.assertIsNone(self.controller.retirement.intent)

    def test_generation_when_ingress_changes_rebaselines(self) -> None:
        self.controller.tick(1)
        self.ingress.generation = "two"
        self.assertEqual(self.controller.tick(2), "baseline")
        self.assertEqual(self.runtime.removed, [])

    def test_stream_when_pool_active_does_not_start_retirement(self) -> None:
        self.ingress.sessions = 1
        self.controller.tick(1)
        self.controller.tick(2)
        self.assertIsNone(self.controller.retirement.intent)

    def test_fairness_when_all_pools_busy_adjusts_independently(self) -> None:
        for sample in range(1, 7):
            self.ingress.requests += 1000
            self.controller.tick(sample)
        self.assertEqual(self.runtime.grown, [("engine", 3), ("web-a", 3), ("web-e", 3)])

    def test_unhealthy_when_other_replica_pending_blocks_retirement(self) -> None:
        self.runtime.current = tuple(replace(item, healthy=False) if item.service == "engine" else item
                                     for item in self.runtime.current)
        self.controller.tick(1)
        result = self.controller.tick(2)
        self.assertIn("service=web-a", result)
        self.assertNotIn("service=engine", result)

    def test_rate_when_elapsed_time_is_long_uses_delta_per_second(self) -> None:
        self.ingress.requests = 1000
        self.controller.tick(100)
        self.ingress.requests = 2000
        self.controller.tick(200)
        self.assertEqual(self.runtime.grown, [])

    def test_cooldown_when_demand_persists_defers_next_adjustment(self) -> None:
        self.controller.policy = replace(self.controller.policy, cooldown_seconds=120)
        for sample in range(1, 10):
            self.ingress.requests += 1000
            self.controller.tick(sample)
        self.assertEqual(self.runtime.grown, [("engine", 3)])
