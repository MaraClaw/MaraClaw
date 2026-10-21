from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deploy.autoscale_docker import Container, Docker
from deploy.autoscale_policy import GIB, Policy
from deploy.test_resources import DEFAULTS


def model() -> str:
    return json.dumps({"name": "test", "networks": {"app": {"name": "test_app"}},
        "services": {name: {"image": "fixture", "mem_limit": limits.memory, "deploy": {"replicas": limits.replicas},
            "volumes": [{"source": "/state", "target": "/run/maraclaw-autoscaler"}]}
            for name, limits in DEFAULTS.items()}})


def container(service: str = "engine") -> Container:
    return Container("a" * 64, "test", service, True, True, "10.0.0.2", 2 * GIB, "start")


class DockerTests(unittest.TestCase):
    def setUp(self) -> None:
        with patch("deploy.autoscale_docker.run", return_value=model()):
            self.docker = Docker(Path("/repo"), (Path("/inputs.env"), Path("/compose.yaml")))

    def test_growth_when_admitted_uses_only_target_without_recreation(self) -> None:
        with patch("deploy.autoscale_docker.run") as command:
            self.docker.grow("web-a", 3)
        command.assert_called_once_with(self.docker.compose + ["up", "-d", "--no-deps", "--no-recreate",
            "--no-build", "--pull", "never", "--scale", "web-a=3", "web-a"], 120)

    def test_removal_when_verified_stops_then_removes_exact_full_identity(self) -> None:
        with patch("deploy.autoscale_docker.run") as command:
            self.docker.remove("a" * 64)
        self.assertEqual([call.args for call in command.call_args_list], [
            (["docker", "stop", "--time", "60", "a" * 64], 75), (["docker", "rm", "a" * 64],)])

    def test_memory_when_container_limit_exceeds_compose_uses_larger_limit(self) -> None:
        with patch("deploy.autoscale_docker.run", return_value=str(32 * GIB)):
            result = self.docker.memory((replace(container(), memory=3 * GIB),), Policy())
        self.assertEqual(result, (32 * GIB, 3 * GIB, 0))

    def test_memory_when_unlimited_external_guest_is_unaccounted_blocks_admission(self) -> None:
        guest = replace(container(), project="guest", memory=0)
        with patch("deploy.autoscale_docker.run", return_value=str(24 * GIB)):
            result = self.docker.memory((container(), guest), Policy())
        self.assertEqual(result, (24 * GIB, 2 * GIB, None))

    def test_memory_when_operator_accounts_unlimited_guests_adds_known_guest_limits(self) -> None:
        guests = (replace(container(), project="guest", memory=0),
                  replace(container(), project="other", memory=3 * GIB))
        with patch("deploy.autoscale_docker.run", return_value=str(24 * GIB)):
            result = self.docker.memory(guests, Policy(external_unlimited_gib=4))
        self.assertEqual(result, (24 * GIB, 0, 7 * GIB))

    def test_startup_when_daemon_is_eight_gib_refuses_default_stack(self) -> None:
        with patch.object(self.docker, "containers", return_value=()) as snapshot, \
                patch("deploy.autoscale_docker.run", return_value=str(8 * GIB)) as command:
            with self.assertRaisesRegex(ValueError, "admission refused") as refused:
                self.docker.startup(Policy())
        snapshot.assert_called_once_with()
        command.assert_called_once_with(["docker", "info", "--format", "{{.MemTotal}}"])
        message = str(refused.exception)
        for detail in ("daemon_capacity=8.000 GiB", "effective_capacity=8.000 GiB",
                       "host_reserve=2.000 GiB", "required_stack=6.750 GiB",
                       "external_reserve=2.000 GiB", "stack_budget=4.000 GiB",
                       "shortfall=2.750 GiB", "Docker memory", "explicitly configured",
                       "--development", "separate local development"):
            self.assertIn(detail, message)

    def test_startup_when_external_allocation_is_unknown_explains_hard_limit_remedy(self) -> None:
        guest = replace(container(), project="guest", memory=0)
        with patch.object(self.docker, "containers", return_value=(guest,)), \
                patch("deploy.autoscale_docker.run", return_value=str(32 * GIB)):
            with self.assertRaisesRegex(ValueError, "admission refused") as refused:
                self.docker.startup(Policy())
        message = str(refused.exception)
        for detail in ("external_reserve=unknown", "unlimited external", "hard limits",
                       "external_unlimited_gib", "shortfall=unknown"):
            self.assertIn(detail, message)

    def test_startup_when_known_external_limits_exceed_floor_reports_actual_reserve(self) -> None:
        guest = replace(container(), project="guest", memory=3 * GIB)
        with patch.object(self.docker, "containers", return_value=(guest,)), \
                patch("deploy.autoscale_docker.run", return_value=str(11 * GIB)):
            with self.assertRaises(ValueError) as refused:
                self.docker.startup(Policy())
        self.assertIn("external_reserve=3.000 GiB", str(refused.exception))
        self.assertIn("shortfall=0.750 GiB", str(refused.exception))

    def test_startup_when_one_byte_short_reports_exact_shortfall(self) -> None:
        total = 43 * GIB // 4 - 1
        with patch.object(self.docker, "containers", return_value=()), \
                patch("deploy.autoscale_docker.run", return_value=str(total)):
            with self.assertRaises(ValueError) as refused:
                self.docker.startup(Policy())
        self.assertIn("shortfall=0.000 GiB (1 bytes)", str(refused.exception))

    def test_startup_when_existing_counts_differ_preserves_stopped_members_not_oneoffs(self) -> None:
        members = tuple(replace(container(), identity=str(index) * 64, running=False) for index in (1, 2, 3))
        members += (replace(container(), oneoff=True),)
        with patch.object(self.docker, "containers", return_value=members), \
                patch("deploy.autoscale_docker.run", return_value=str(24 * GIB)):
            counts = self.docker.startup(Policy())
        self.assertEqual(counts, {"engine": 3, "web-a": 2, "web-e": 2})

    def test_startup_when_compose_override_increases_memory_rejects_over_budget(self) -> None:
        self.docker.services["engine"] = replace(self.docker.services["engine"], memory=9 * GIB // 2)
        with patch.object(self.docker, "containers", return_value=()), \
                patch("deploy.autoscale_docker.run", return_value=str(16 * GIB)):
            with self.assertRaisesRegex(ValueError, "admission refused"):
                self.docker.startup(Policy())

    def test_startup_when_capacity_is_capped_cannot_borrow_daemon_memory(self) -> None:
        with patch.object(self.docker, "containers", return_value=()), \
                patch("deploy.autoscale_docker.run", return_value=str(32 * GIB)):
            with self.assertRaisesRegex(ValueError, "admission refused") as refused:
                self.docker.startup(Policy(capacity_gib=10))
        self.assertIn("daemon_capacity=32.000 GiB", str(refused.exception))
        self.assertIn("effective_capacity=10.000 GiB", str(refused.exception))
        self.assertIn("shortfall=0.750 GiB", str(refused.exception))

    def test_startup_when_stopped_project_member_is_unlimited_refuses_start(self) -> None:
        members = (replace(container(), running=False, memory=0),)
        with patch.object(self.docker, "containers", return_value=members), \
                patch("deploy.autoscale_docker.run", return_value=str(32 * GIB)):
            with self.assertRaisesRegex(ValueError, "unlimited project container"):
                self.docker.startup(Policy())


class BootstrapRerunTests(unittest.TestCase):
    def setUp(self) -> None:
        with patch("deploy.autoscale_docker.run", return_value=model()):
            self.docker = Docker(Path("/repo"), (Path("/inputs.env"), Path("/compose.yaml")))
        services = [(name, limits.memory) for name, limits in DEFAULTS.items()
                    for _replica in range(limits.replicas) if name != "bootstrap"]
        members = tuple(replace(container(name), identity=f"{index:064x}", memory=memory,
                                config_hash="unchanged", image="fixture-image")
                        for index, (name, memory) in enumerate(services, 1))
        bootstrap = replace(container("bootstrap"), running=False, memory=GIB, exit_code=0,
                            config_hash="unchanged", image="fixture-image")
        self.members = members + (bootstrap,)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.docker.state_dir = Path(temporary.name)
        self.docker.root = Path(temporary.name)
        (self.docker.root / "deploy").mkdir()
        (self.docker.root / "deploy/bootstrap.py").write_text("bootstrap version one")
        self.image = "fixture-image"
        self.config_hash = "unchanged"
        commands = patch("deploy.autoscale_docker.run", side_effect=self.command)
        inspection = patch.object(self.docker, "containers", side_effect=lambda: self.members)
        commands.start()
        inspection.start()
        self.addCleanup(commands.stop)
        self.addCleanup(inspection.stop)
        self.docker.remember_startup()

    def command(self, arguments: list[str], timeout: int = 30) -> str:
        if arguments[1] == "info":
            return str(11 * GIB)
        if arguments[1] == "image":
            return self.image
        return "\n".join(f"{name} {self.config_hash}" for name in DEFAULTS)

    def test_startup_when_successful_checkpoint_matches_admits_eleven_gib_without_bootstrap(self) -> None:
        counts = self.docker.startup(Policy())
        self.assertEqual(counts, {"engine": 2, "web-a": 2, "web-e": 2})
        self.assertTrue(self.docker.startup_reusable)

    def test_startup_when_autoscaling_changed_routed_counts_preserves_them_without_bootstrap(self) -> None:
        member = next(item for item in self.members if item.service == "web-a")
        self.members += (replace(member, identity="f" * 64),)
        self.assertEqual(self.docker.startup(Policy())["web-a"], 3)
        self.assertTrue(self.docker.startup_reusable)

    def test_startup_when_bootstrap_source_changed_reserves_overlap(self) -> None:
        (self.docker.root / "deploy/bootstrap.py").write_text("bootstrap version two")
        with self.assertRaisesRegex(ValueError, "admission refused"):
            self.docker.startup(Policy())

    def test_startup_when_built_image_changed_reserves_overlap(self) -> None:
        self.image = "rebuilt-image"
        with self.assertRaisesRegex(ValueError, "admission refused"):
            self.docker.startup(Policy())

    def test_startup_when_rendered_configuration_changed_reserves_overlap(self) -> None:
        self.config_hash = "new-configuration"
        with self.assertRaisesRegex(ValueError, "admission refused"):
            self.docker.startup(Policy())

    def test_startup_when_replica_is_stopped_reserves_overlap(self) -> None:
        self.members = (replace(self.members[0], running=False), *self.members[1:])
        self.docker.startup(Policy())
        self.assertFalse(self.docker.startup_reusable)

    def test_record_when_bootstrap_failed_never_records_success(self) -> None:
        self.members = tuple(replace(item, exit_code=1) if item.service == "bootstrap" else item
                             for item in self.members)
        (self.docker.state_dir / "startup.sha256").unlink()
        with self.assertRaisesRegex(ValueError, "bootstrap has not completed"):
            self.docker.remember_startup()
        self.assertFalse((self.docker.state_dir / "startup.sha256").exists())
