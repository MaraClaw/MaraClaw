from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deploy.autoscale_development import prepare, validate
from deploy.autoscale_docker import Docker, environment
from deploy.autoscale_policy import GIB, Policy
from deploy.test_resources import REQUIRED_INPUTS
from deploy.test_autoscale_docker import container
from deploy.autoscale_controller import Controller
from deploy.autoscale_proxy import Snapshot
from deploy.test_autoscale_controller import RuntimeProbe, IngressProbe


ROOT = Path(__file__).resolve().parents[1]


class DevelopmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.state = self.directory / "state"
        self.state.mkdir(mode=0o700)
        self.env = self.directory / "input.env"
        self.values = dict(REQUIRED_INPUTS)
        self.values["MARACLAW_ENV_FILE"] = str(self.env)
        self.env.write_text("\n".join(f"{key}={value}" for key, value in self.values.items()))
        self.files = (self.env, ROOT / "compose.yaml", ROOT / "deploy/compose.macos.yaml")
        self.environment = patch.dict(os.environ, {"DOCKER_HOST": "unix:///fixture/docker.sock",
                                                  "MARACLAW_ENV_FILE": str(self.env)})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def docker(self) -> Docker:
        initial = Docker(ROOT, self.files)
        from deploy.autoscale_docker import run
        values = dict(line.split("=", 1) for line in run(initial.compose + ["config", "--environment"]).splitlines()
                      if "=" in line)
        override = prepare(self.files, self.state, values)
        return Docker(ROOT, (self.env, self.files[1], override, *self.files[2:]), self.state)

    def test_fresh_limits_admit_real_small_daemon_without_changing_production(self) -> None:
        docker = self.docker()
        with patch.object(docker, "containers", return_value=()), \
                patch("deploy.autoscale_docker.run", return_value=str(int(7.748 * GIB))):
            self.assertEqual(docker.startup(Policy()), {"engine": 1, "web-a": 1, "web-e": 1})
        self.assertEqual(sum(spec.memory for name, spec in docker.services.items() if name != "bootstrap"),
                         int(1.875 * GIB))
        self.assertEqual(docker.services["bootstrap"].memory, int(.75 * GIB))
        self.assertEqual(set(docker.services), {"engine", "background", "bootstrap", "web-a", "web-e", "ingress"})

    def test_production_valued_explicit_override_is_not_reduced(self) -> None:
        with self.env.open("a") as stream:
            stream.write("\nENGINE_MEMORY=2g\nAPI_REPLICAS=2\n")
        docker = self.docker()
        self.assertEqual((docker.services["engine"].memory, docker.services["engine"].replicas), (2 * GIB, 2))

    def test_production_subprocesses_do_not_inherit_shell_resource_defaults(self) -> None:
        overrides = {"ENGINE_MEMORY": "64m", "API_REPLICAS": "16", "SECRET_KEY": "private",
                     "DATABASE_URL": "postgresql://shell/wrong", "REDIS_URL": "redis://shell/3"}
        with patch.dict(os.environ, overrides):
            self.assertFalse(overrides.keys() & environment().keys())

    def test_runtime_and_development_compose_keep_env_file_urls_despite_shell_conflicts(self) -> None:
        from deploy.autoscale_docker import run
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://shell/wrong", "REDIS_URL": "redis://shell/3"}):
            for docker in (Docker(ROOT, self.files), self.docker()):
                model = json.loads(run(docker.compose + ["config", "--format", "json", "--no-env-resolution"]))
                for name in ("engine", "background", "bootstrap"):
                    for key in ("DATABASE_URL", "REDIS_URL"):
                        self.assertEqual(model["services"][name]["environment"][key], self.values[key])

    def test_environment_override_is_persisted_and_changed_value_refused(self) -> None:
        with patch.dict(os.environ, {"ENGINE_MEMORY": "1536m", "API_REPLICAS": "3", "ENGINE_CPUS": ".75"}):
            docker = self.docker()
            self.assertEqual((docker.services["engine"].memory, docker.services["engine"].replicas),
                             (int(1.5 * GIB), 3))
        self.assertEqual(self.docker().services["engine"], docker.services["engine"])
        with patch.dict(os.environ, {"ENGINE_MEMORY": "2g"}), self.assertRaisesRegex(ValueError, "settings changed"):
            self.docker()

    def test_unchanged_rerun_reuses_identical_atomic_override(self) -> None:
        self.docker()
        path = self.state / "development.json"
        before = path.read_bytes(), path.stat().st_mtime_ns
        self.docker()
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(self.values["SECRET_KEY"], path.read_text())

    def test_altered_override_or_inputs_fail_closed(self) -> None:
        self.docker()
        path = self.state / "development.json"
        original = path.read_text()
        path.write_text(original.replace('"768m"', '"64m"'))
        with self.assertRaisesRegex(ValueError, "override changed"):
            validate(path)
        path.write_text(original)
        self.env.write_text(self.env.read_text() + "\nENGINE_MEMORY=1g\n")
        with self.assertRaisesRegex(ValueError, "input changed"):
            validate(path)

    def test_ordered_files_and_host_state_are_verified_by_runtime(self) -> None:
        self.docker()
        path = self.state / "development.json"
        with self.assertRaisesRegex(ValueError, "ordered files"):
            Docker(ROOT, (self.env, self.files[1], self.files[2], path), self.state)
        with self.assertRaisesRegex(ValueError, "ordered files"):
            Docker(ROOT, (self.env, self.files[1], path, self.files[2]), self.directory)

    def test_existing_production_container_is_never_adopted(self) -> None:
        docker = self.docker()
        members = (replace(container(), project=docker.project),)
        with patch.object(docker, "containers", return_value=members), self.assertRaisesRegex(ValueError, "cannot adopt"):
            docker.startup(Policy())

    def test_runtime_cannot_omit_effective_file_or_substitute_env_file(self) -> None:
        docker = self.docker()
        alternate = self.directory / "alternate.env"
        alternate.write_bytes(self.env.read_bytes())
        with self.assertRaisesRegex(ValueError, "ordered files"):
            Docker(ROOT, (alternate, *docker.files), self.state)
        with self.assertRaisesRegex(ValueError, "effective file omitted"):
            Docker(ROOT, self.files, self.state)

    def test_growth_refuses_changed_input_or_missing_effective_file(self) -> None:
        docker = self.docker()
        self.env.write_text(self.env.read_text() + "\nENGINE_MEMORY=1g\n")
        with patch("deploy.autoscale_docker.run") as run, self.assertRaisesRegex(ValueError, "input changed"):
            docker.grow("engine", 2)
        run.assert_not_called()
        (self.state / "development.json").unlink()
        with patch("deploy.autoscale_docker.run") as run, self.assertRaises(FileNotFoundError):
            docker.grow("engine", 2)
        run.assert_not_called()

    def test_record_refuses_inputs_changed_after_startup(self) -> None:
        docker = self.docker()
        bootstrap = replace(container("bootstrap"), project=docker.project, running=False, exit_code=0)
        self.env.write_text(self.env.read_text() + "\nENGINE_MEMORY=1g\n")
        with patch.object(docker, "containers", return_value=(bootstrap,)), \
                patch("deploy.autoscale_docker.run") as run, self.assertRaisesRegex(ValueError, "input changed"):
            docker.remember_startup()
        run.assert_not_called()
        self.assertFalse((self.state / "startup.sha256").exists())

    def test_corruption_blocks_new_retirement_and_pending_drain(self) -> None:
        for pending in (False, True):
            with self.subTest(pending=pending):
                docker = self.docker()
                runtime = RuntimeProbe()
                runtime.current = tuple(replace(item, project=docker.project) for item in runtime.current)
                proxy = IngressProbe(runtime)
                controller = Controller(docker, proxy, Policy(low_samples=2), self.state)
                with patch.object(docker, "containers", side_effect=runtime.containers):
                    controller.tick(0)
                    controller.tick(1)
                    if pending:
                        controller.tick(2)
                        self.assertIsNotNone(controller.retirement.intent)
                    original = self.env.read_bytes()
                    self.env.write_bytes(original + b"\nENGINE_MEMORY=1g\n")
                    with patch("deploy.autoscale_docker.run") as command:
                        with self.assertRaisesRegex(ValueError, "input changed"):
                            controller.tick(3)
                        controller.invalidate()
                    command.assert_not_called()
                    self.assertIsNone(controller.retirement.intent)
                    self.assertFalse((self.state / "pending.json").exists())
                    if pending:
                        self.assertEqual(proxy.states[-1][-1], "ready")
                    else:
                        self.assertEqual(proxy.states, [])
                    self.env.write_bytes(original)

    def test_corruption_during_final_retirement_verification_blocks_stop_and_rm(self) -> None:
        docker = self.docker()
        runtime = RuntimeProbe()
        runtime.current = tuple(replace(item, project=docker.project) for item in runtime.current)
        proxy = IngressProbe(runtime)
        controller = Controller(docker, proxy, Policy(low_samples=2), self.state)
        with patch.object(docker, "containers", side_effect=runtime.containers):
            for now in range(4):
                controller.tick(now)
            self.assertEqual(controller.retirement.quiet, 1)
            snapshot = proxy.snapshot
            calls = 0

            def corrupt_on_confirmation(ingress: str) -> Snapshot:
                nonlocal calls
                calls += 1
                if calls == 2:
                    (self.state / "development.json").unlink()
                return snapshot(ingress)

            with patch.object(proxy, "snapshot", side_effect=corrupt_on_confirmation), \
                    patch("deploy.autoscale_docker.run") as command:
                with self.assertRaises(FileNotFoundError):
                    controller.tick(4)
                controller.invalidate()
            self.assertEqual(calls, 2)
            command.assert_not_called()
            self.assertIsNone(controller.retirement.intent)
            self.assertFalse((self.state / "pending.json").exists())
            self.assertEqual(proxy.states[-1][-1], "ready")

    def test_input_change_while_stop_runs_prevents_subsequent_rm(self) -> None:
        docker = self.docker()

        def stop_then_corrupt(command: list[str], timeout: int = 30) -> str:
            self.env.write_text(self.env.read_text() + "\nENGINE_MEMORY=1g\n")
            return ""

        with patch("deploy.autoscale_docker.run", side_effect=stop_then_corrupt) as command:
            with self.assertRaisesRegex(ValueError, "input changed"):
                docker.remove("a" * 64)
        command.assert_called_once_with(["docker", "stop", "--time", "60", "a" * 64], 75)

    def test_existing_development_replica_counts_survive_stopped_members(self) -> None:
        docker = self.docker()
        assert docker.development is not None
        members = tuple(replace(container(), project=docker.project, memory=int(.75 * GIB),
                                running=False, development=docker.development["token"], identity=f"{index:064x}")
                        for index in range(3))
        with patch.object(docker, "containers", return_value=members), \
                patch("deploy.autoscale_docker.run", return_value=str(16 * GIB)):
            self.assertEqual(docker.startup(Policy())["engine"], 3)

    def test_bootstrap_overlap_and_unknown_external_allocations_cannot_be_bypassed(self) -> None:
        docker = self.docker()
        with patch.object(docker, "containers", return_value=()), \
                patch("deploy.autoscale_docker.run", return_value=str(int(6.625 * GIB) - 1)), \
                self.assertRaisesRegex(ValueError, "admission refused"):
            docker.startup(Policy())
        guest = replace(container(), project="other", memory=0)
        with patch.object(docker, "containers", return_value=(guest,)), \
                patch("deploy.autoscale_docker.run", return_value=str(32 * GIB)), \
                self.assertRaisesRegex(ValueError, "admission refused"):
            docker.startup(Policy())

    def test_bootstrap_overlap_fits_at_exact_admission_boundary(self) -> None:
        docker = self.docker()
        self.assertEqual(sum(spec.memory for spec in docker.services.values()), int(2.625 * GIB))
        with patch.object(docker, "containers", return_value=()), \
                patch("deploy.autoscale_docker.run", return_value=str(int(6.625 * GIB))):
            self.assertEqual(docker.startup(Policy()), {"engine": 1, "web-a": 1, "web-e": 1})

    def test_socket_volume_is_project_named_and_shared_only_with_background(self) -> None:
        docker = self.docker()
        from deploy.autoscale_docker import run
        model = json.loads(run(docker.compose + ["config", "--format", "json", "--no-env-resolution"]))
        socket = model["volumes"]["autoscaler_socket"]["name"]
        self.assertEqual(socket, docker.project + "_autoscaler_socket")
        for name, service in model["services"].items():
            matches = [mount for mount in service.get("volumes", []) if mount["target"] == "/run/maraclaw-autoscaler"]
            self.assertEqual(len(matches), int(name in {"ingress", "background"}))
        for name in ("engine", "background", "bootstrap"):
            self.assertEqual(model["services"][name]["env_file"], [{"path": str(self.env), "required": False}])
