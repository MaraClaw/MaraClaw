"""Resource contracts against actual Compose JSON; no containers are started."""

import json
import os
import subprocess
import tempfile
import unittest
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, NotRequired, TypedDict

ROOT: Final = Path(__file__).resolve().parents[1]
MIB: Final = 1024**2
GIB: Final = 1024**3
REQUIRED_INPUTS: Final[Mapping[str, str]] = {
    "SECRET_KEY": "resource-test-secret",
    "JWT_SECRET_KEY": "resource-test-jwt-secret",
    "POSTGRES_PASSWORD": "resource-test-postgres",
    "POSTGRES_PASSWORD_URLENCODED": "resource-test-postgres",
    "REDIS_PASSWORD": "resource-test-redis",
    "REDIS_PASSWORD_URLENCODED": "resource-test-redis",
    "PLATFORM_ADMIN_EMAIL": "resources@example.test",
    "PLATFORM_ADMIN_PASSWORD": "resource-test-admin-password",
    "INSTANCE_ID": "resource-test-instance",
    "API_DOMAIN": "api.example.test",
    "ADMIN_DOMAIN": "admin.example.test",
    "MEMBER_DOMAIN": "members.example.test",
    "DATA_DIR": "/srv/resource-test",
    "TLS_PEM_PATH": "/srv/resource-test-tls.pem",
}
RESOURCE_KEYS: Final = frozenset(
    f"{prefix}_{suffix}"
    for prefix in ("ENGINE", "BACKGROUND", "BOOTSTRAP", "POSTGRES", "REDIS", "WEB", "INGRESS")
    for suffix in ("CPUS", "MEMORY")
)


@dataclass(frozen=True, slots=True)
class Limits:
    cpus: float
    memory: int
    replicas: int = 1


DEFAULTS: Final[Mapping[str, Limits]] = {
    "engine": Limits(1, 2 * GIB, 2),
    "background": Limits(1, 2 * GIB),
    "bootstrap": Limits(1, GIB),
    "postgres": Limits(1.5, 3 * GIB),
    "redis": Limits(0.5, GIB),
    "web-a": Limits(0.25, 128 * MIB, 2),
    "web-e": Limits(0.25, 128 * MIB, 2),
    "ingress": Limits(0.5, 256 * MIB),
}
STEADY_SERVICES: Final = tuple(name for name in DEFAULTS if name != "bootstrap")
STARTUP_SERVICES: Final = tuple(name for name in DEFAULTS if name not in {"engine", "background"})


class ReplicaConfig(TypedDict):
    replicas: NotRequired[int]


class ServiceConfig(TypedDict):
    cpus: float
    mem_limit: str | int
    deploy: NotRequired[ReplicaConfig]


class ComposeConfig(TypedDict):
    services: dict[str, ServiceConfig]


def render(overrides: Mapping[str, str]) -> Mapping[str, Limits]:
    """Render isolated synthetic inputs, ignoring operator env files and controls."""
    with tempfile.TemporaryDirectory(prefix="maraclaw-resource-test-") as directory:
        env_file = Path(directory) / ".env"
        values = dict(REQUIRED_INPUTS) | dict(overrides)
        env_file.write_text("\n".join(f"{key}='{value}'" for key, value in values.items()), encoding="utf-8")
        environment = {key: value for key, value in os.environ.items() if key in {
            "PATH", "HOME", "DOCKER_CONFIG", "DOCKER_HOST", "DOCKER_CONTEXT", "TMPDIR",
        }}
        result = subprocess.run(
            ["docker", "compose", "--env-file", str(env_file), "-f", str(ROOT / "compose.yaml"),
             "config", "--format", "json", "--no-env-resolution"],
            cwd=ROOT, env=environment, capture_output=True, text=True, timeout=30, check=True,
        )
    # Compose owns this JSON schema; project only its effective resource fields.
    model: ComposeConfig = json.loads(result.stdout)
    return {
        name: Limits(service["cpus"], int(service["mem_limit"]), service.get("deploy", {}).get("replicas", 1))
        for name, service in model["services"].items()
    }


def aggregate(services: Mapping[str, Limits], names: Iterable[str]) -> tuple[float, int]:
    """Weight each effective per-container limit by its rendered replica count."""
    limits = tuple(services[name] for name in names)
    return (
        sum(limit.cpus * limit.replicas for limit in limits),
        sum(limit.memory * limit.replicas for limit in limits),
    )


class ResourceContracts(unittest.TestCase):
    def test_effective_limits_when_node_version_is_overridden(self) -> None:
        services = render({"WEB_NODE_VERSION": "26.10.0"})
        self.assertEqual(services, DEFAULTS)

    def test_effective_limits_when_resource_controls_are_omitted(self) -> None:
        # Given synthetic required inputs without optional resource controls.
        # When Compose resolves defaults and YAML inheritance.
        services = render({})
        # Then every service has its intended per-container limit and replicas.
        for name, expected in DEFAULTS.items():
            with self.subTest(service=name):
                self.assertEqual(services[name], expected)

    def test_steady_aggregate_when_default_replicas_are_running(self) -> None:
        # Given the steady services (completed bootstrap is excluded).
        # When Compose renders their effective limits and replicas.
        services = render({})
        # Then aggregate limits are 6.5 CPUs and 10.75 GiB.
        self.assertEqual(aggregate(services, STEADY_SERVICES), (6.5, 43 * GIB // 4))

    def test_host_headroom_when_default_replicas_are_running(self) -> None:
        # Given an 8-CPU / 16-GiB host and the steady services.
        # When Compose renders the replica-weighted limits.
        cpus, memory = aggregate(render({}), STEADY_SERVICES)
        # Then 1.5 CPUs and 5.25 GiB remain outside this stack's limits.
        self.assertEqual((8 - cpus, 16 * GIB - memory), (1.5, 21 * GIB // 4))

    def test_startup_aggregate_when_bootstrap_is_the_only_engine_role(self) -> None:
        # Given bootstrap plus all stores, web replicas and ingress, conservatively.
        # When Compose renders their limits, without API/background consumers.
        services = render({})
        # Then this phase's upper bound is 4.5 CPUs and 5.75 GiB.
        self.assertEqual(aggregate(services, STARTUP_SERVICES), (4.5, 23 * GIB // 4))

    def test_singleton_defaults_when_only_engine_limits_are_overridden(self) -> None:
        # Given API limits distinct from both singleton defaults.
        overrides = {"ENGINE_CPUS": "2.25", "ENGINE_MEMORY": "2304m"}
        # When Compose renders the API-only override.
        services = render(overrides)
        # Then background/bootstrap and other services retain their own budgets.
        self.assertEqual(services, dict(DEFAULTS) | {"engine": Limits(2.25, 2304 * MIB, 2)})

    def test_api_defaults_when_only_singleton_limits_are_overridden(self) -> None:
        # Given background/bootstrap overrides distinct from each other and API.
        overrides = {
            "BACKGROUND_CPUS": "0.75", "BACKGROUND_MEMORY": "1792m",
            "BOOTSTRAP_CPUS": "0.625", "BOOTSTRAP_MEMORY": "768m",
        }
        # When Compose renders the singleton-only overrides.
        services = render(overrides)
        # Then only the independently controlled singleton limits change.
        self.assertEqual(services, dict(DEFAULTS) | {
            "background": Limits(0.75, 1792 * MIB),
            "bootstrap": Limits(0.625, 768 * MIB),
        })

    def test_effective_limits_when_all_resource_controls_are_overridden(self) -> None:
        # Given every control with a value distinct from its default and siblings.
        overrides = {
            "ENGINE_CPUS": "1.25", "ENGINE_MEMORY": "2304m",
            "BACKGROUND_CPUS": "0.75", "BACKGROUND_MEMORY": "1792m",
            "BOOTSTRAP_CPUS": "0.625", "BOOTSTRAP_MEMORY": "768m",
            "POSTGRES_CPUS": "1.75", "POSTGRES_MEMORY": "3584m",
            "REDIS_CPUS": "0.375", "REDIS_MEMORY": "896m",
            "WEB_CPUS": "0.125", "WEB_MEMORY": "160m",
            "INGRESS_CPUS": "0.875", "INGRESS_MEMORY": "320m",
        }
        # When Compose renders all override inputs together.
        services = render(overrides)
        # Then each service selects its own controls, with shared web controls only.
        self.assertEqual(services, {
            "engine": Limits(1.25, 2304 * MIB, 2),
            "background": Limits(0.75, 1792 * MIB),
            "bootstrap": Limits(0.625, 768 * MIB),
            "postgres": Limits(1.75, 3584 * MIB),
            "redis": Limits(0.375, 896 * MIB),
            "web-a": Limits(0.125, 160 * MIB, 2),
            "web-e": Limits(0.125, 160 * MIB, 2),
            "ingress": Limits(0.875, 320 * MIB),
        })

    def test_aggregate_when_routed_replica_counts_are_overridden(self) -> None:
        # Given distinct API/admin/member replica counts of 3/4/5.
        overrides = {"API_REPLICAS": "3", "ADMIN_REPLICAS": "4", "MEMBER_REPLICAS": "5"}
        # When Compose renders the scaled topology.
        services = render(overrides)
        # Then per-replica limits produce 8.75 CPUs and 13.375 GiB steady aggregate.
        self.assertEqual(aggregate(services, STEADY_SERVICES), (8.75, 107 * GIB // 8))

    def test_effective_defaults_when_example_resource_controls_are_loaded(self) -> None:
        # Given only the optional resource controls from the shipped env example.
        controls = dict(
            line.split("=", 1)
            for line in (ROOT / "deploy" / ".env.example").read_text(encoding="utf-8").splitlines()
            if line.partition("=")[0] in RESOURCE_KEYS
        )
        # When Compose renders those controls with synthetic required inputs.
        services = render(controls)
        # Then all controls are exposed and yield the same effective default limits.
        self.assertEqual(controls.keys(), RESOURCE_KEYS)
        self.assertEqual(services, DEFAULTS)


if __name__ == "__main__":
    unittest.main()
