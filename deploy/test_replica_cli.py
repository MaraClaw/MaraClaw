from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from deploy import test_autoscale_starter as fixtures


class ReplicaCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.host = fixtures.StarterTests()
        self.host.setUp()
        self.addCleanup(self.host.doCleanups)
        self.root = self.host.root
        self.state = self.root / "state"
        binary = self.root / "bin/docker"
        binary.write_text(f"#!{sys.executable}\n" +
                          (Path(__file__).parent / "starter_host_fixture.py").read_text())
        binary.chmod(0o700)
        self.command = [sys.executable, "-B", "-m", "deploy.autoscale", "--root", str(self.root),
            "--state-dir", str(self.state), "--env-file", str(self.root / "deploy/.env"),
            "--compose-file", str(self.root / "compose.yaml"), "--docker-host", "unix:///fixture/docker.sock",
            "--docker-binary", str(binary)]

    def invoke(self, option: str) -> subprocess.CompletedProcess[str]:
        (self.root / "fixture.json").write_text(json.dumps(self.host.fixture))
        environment = dict(os.environ)
        environment.pop("DOCKER_CONTEXT", None)
        return subprocess.run([*self.command, option], env=environment, capture_output=True,
                              text=True, timeout=30, check=False)

    def admit(self) -> None:
        result = self.invoke("--check-startup")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_fresh_cli_when_partial_pool_disappears_preserves_checkpoint_bytes_and_mtime(self) -> None:
        self.admit()
        self.assertEqual(self.invoke("--record-startup").returncode, 0)
        checkpoint = self.state / "startup.sha256"
        previous = checkpoint.read_bytes(), checkpoint.stat().st_mtime_ns
        original = self.host.fixture["containers"]
        for service in ("engine", "web-a", "web-e"):
            with self.subTest(service=service):
                victim = next(item for item in original if item["Config"]["Labels"]["com.docker.compose.service"] == service)
                self.host.fixture["containers"] = [item for item in original if item is not victim]
                self.assertEqual(self.invoke("--record-startup").returncode, 1)
                self.assertEqual((checkpoint.read_bytes(), checkpoint.stat().st_mtime_ns), previous)

    def test_fresh_cli_when_initial_admission_authorizes_one_or_three_ignores_defaults_two(self) -> None:
        original = deepcopy(self.host.fixture["containers"])
        for count in (1, 3):
            with self.subTest(count=count):
                victim = next(item for item in original if item["Config"]["Labels"]["com.docker.compose.service"] == "engine")
                members = [item for item in original if item["Config"]["Labels"]["com.docker.compose.service"] != "engine"]
                for index in range(count):
                    member = deepcopy(victim)
                    member["Id"] = f"{100 + index:064x}"
                    members.append(member)
                self.host.fixture["containers"] = members
                (self.state / "replicas.json").unlink(missing_ok=True)
                self.admit()
                self.assertEqual(self.invoke("--record-startup").returncode, 0)
                counts = json.loads((self.state / "replicas.json").read_text())["counts"]
                self.assertEqual(counts["engine"], count)

    def test_direct_shortcuts_when_controller_lock_busy_never_admit_or_record(self) -> None:
        with (self.state / "controller.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for option in ("--check-startup", "--record-startup"):
                with self.subTest(option=option):
                    result = self.invoke(option)
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        events = self.host.events()
        self.assertFalse(any("{{.MemTotal}}" in args or "--hash" in args for _, args in events))
        self.assertFalse((self.state / "replicas.json").exists())
        self.assertFalse((self.state / "startup.sha256").exists())

    def test_record_when_no_admission_exists_cannot_create_count_authority(self) -> None:
        self.assertEqual(self.invoke("--record-startup").returncode, 1)
        self.assertFalse((self.state / "replicas.json").exists())
        self.assertFalse((self.state / "startup.sha256").exists())
