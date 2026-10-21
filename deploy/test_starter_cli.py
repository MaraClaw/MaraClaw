import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from deploy import test_autoscale_starter as fixtures


@unittest.skipUnless(os.uname().sysname == "Darwin", "Darwin CLI with disposable stub service manager")
class StarterCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.host = fixtures.StarterTests()
        self.host.setUp()
        self.addCleanup(self.host.doCleanups)
        self.root = self.host.root
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.state = self.home / "Library/Application Support/MaraClaw/autoscaler"
        for name in ("docker", "systemctl", "launchctl", "python3"):
            binary = self.root / "bin" / name
            binary.write_text(f"#!{sys.executable}\n" + (fixtures.ROOT / "deploy/starter_host_fixture.py").read_text())
            binary.chmod(0o700)
        (self.root / "deploy/compose.macos.yaml").write_text("{}")
        self.environment = dict(os.environ, HOME=str(self.home),
                                PATH=str(self.root / "bin") + os.pathsep + os.environ["PATH"],
                                DOCKER_HOST="unix:///fixture/docker.sock", PYTHONDONTWRITEBYTECODE="1")
        self.environment.pop("DOCKER_CONTEXT", None)

    def invoke(self, *options: str) -> subprocess.CompletedProcess[str]:
        (self.root / "fixture.json").write_text(json.dumps(self.host.fixture))
        return subprocess.run(["bash", str(self.root / "start-docker-compose.sh"), *options],
                              env=self.environment, text=True, capture_output=True, timeout=60, check=False)

    def test_real_cli_rerun_preserves_scaled_counts_and_waits_before_supervisor_bootstrap(self) -> None:
        first = self.invoke()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        (self.root / "events.jsonl").unlink()
        self.host.fixture["memory"] = 16 * fixtures.GIB
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        events = self.host.events()
        stop = next(index for index, (name, arguments) in enumerate(events)
                    if name == "launchctl" and arguments[0] == "bootout")
        start = next(index for index, (name, arguments) in enumerate(events) if name == "docker" and "up" in arguments)
        install = next(index for index, (name, arguments) in enumerate(events)
                       if name == "launchctl" and arguments[0] == "bootstrap")
        self.assertLess(stop, start)
        self.assertLess(start, install)
        arguments = events[start][1]
        self.assertIn("--no-deps", arguments)
        self.assertNotIn("bootstrap", arguments)
        self.assertEqual(arguments[arguments.index("--wait-timeout") + 1], "240")
        self.assertEqual([arguments[index + 1] for index, word in enumerate(arguments) if word == "--scale"],
                         ["engine=2", "web-a=3", "web-e=1"])
        self.assertTrue((self.root / "loaded.plist").exists())

    def test_real_cli_failed_readiness_preserves_checkpoint_and_recovers_supervision_on_retry(self) -> None:
        first = self.invoke()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        checkpoint = self.state / "startup.sha256"
        previous = checkpoint.read_bytes(), checkpoint.stat().st_mtime_ns
        self.host.fixture["fail_up"] = True
        result = self.invoke()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual((checkpoint.read_bytes(), checkpoint.stat().st_mtime_ns), previous)
        self.assertFalse((self.root / "loaded.plist").exists())
        self.host.fixture["fail_up"] = False
        recovered = self.invoke()
        self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
        self.assertTrue((self.root / "loaded.plist").exists())
