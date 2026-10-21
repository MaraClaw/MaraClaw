import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import uuid

from deploy.test_resources import DEFAULTS, GIB
from deploy.autoscale_docker import Docker
from deploy.autoscale_policy import Policy
from deploy.autoscale_state import Authority


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("MARACLAW_STARTER_TEST") == "1",
                     "opt-in isolated Linux starter fixture; requires cached maraclaw-engine:local")
class StarterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "deploy").mkdir()
        (self.root / "bin").mkdir()
        (self.root / "state").mkdir(mode=0o700)
        shutil.copy2(ROOT / "start-docker-compose.sh", self.root)
        for source in (*(ROOT / "deploy").glob("autoscale*"), *(ROOT / "deploy").glob("starter*.py")):
            if source.is_file():
                shutil.copy2(source, self.root / "deploy" / source.name)
        for filename in ("__init__.py", "bootstrap.py", "maraclaw-autoscaler.service", "autoscaler.toml"):
            shutil.copy2(ROOT / "deploy" / filename, self.root / "deploy" / filename)
        for name in ("docker", "systemctl", "python3"):
            target = self.root / "bin" / name
            target.write_text("#!/usr/local/bin/python3\n" + (ROOT / "deploy/starter_host_fixture.py").read_text())
            target.chmod(0o755)
        services = {name: {"image": "fixture", "mem_limit": limits.memory,
                           "deploy": {"replicas": limits.replicas}}
                    for name, limits in DEFAULTS.items()}
        services["ingress"]["volumes"] = [{"source": "/fixture/state", "target": "/run/maraclaw-autoscaler"}]
        counts = {"engine": 2, "web-a": 3, "web-e": 1}
        containers = []
        for service, limits in DEFAULTS.items():
            for _replica in range(counts.get(service, 1)):
                containers.append({"Id": f"{len(containers) + 1:064x}", "Image": "fixture-image",
                    "State": {"Running": service != "bootstrap", "StartedAt": "start", "ExitCode": 0,
                              "Health": {"Status": "healthy"}},
                    "HostConfig": {"Memory": limits.memory}, "NetworkSettings": {"Networks": {}},
                    "Config": {"Labels": {"com.docker.compose.project": "fixture",
                        "com.docker.compose.service": service, "com.docker.compose.config-hash": "unchanged"}}})
        self.fixture = {"model": {"name": "fixture", "services": services,
                                   "networks": {"app": {"name": "fixture_app"}}},
                        "containers": containers, "memory": 24 * GIB, "fail_up": False}
        (self.root / "deploy/.env").touch(mode=0o600)
        (self.root / "compose.yaml").write_text("{}")

    def execute(self) -> subprocess.CompletedProcess[str]:
        (self.root / "fixture.json").write_text(json.dumps(self.fixture))
        name = "maraclaw-starter-" + uuid.uuid4().hex[:12]
        command = ["docker", "run", "--rm", "--name", name, "--network", "none", "--read-only",
                   "--memory", "256m", "--tmpfs", "/run", "--tmpfs", "/tmp",
                   "--tmpfs", "/etc/systemd/system", "--mount", f"type=bind,source={self.root},target=/fixture",
                   "--entrypoint", "/bin/bash", "maraclaw-engine:local", "-c",
                   "mkdir -p /run/systemd/system; export PATH=/fixture/bin:$PATH; "
                   "if [ -f /fixture/controller.service ]; then cp /fixture/controller.service /etc/systemd/system/maraclaw-autoscaler.service; fi; "
                   "export PYTHONDONTWRITEBYTECODE=1; cd /fixture; bash ./start-docker-compose.sh"]
        try:
            return subprocess.run(command, text=True, capture_output=True, timeout=90, check=False)
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "stop", "--time", "1", name], capture_output=True, timeout=10, check=False)
            raise

    def events(self) -> list[tuple[str, list[str]]]:
        return [(name, arguments) for name, arguments in
                (json.loads(line) for line in (self.root / "events.jsonl").read_text().splitlines())]

    def test_starter_when_unchanged_scaled_deployment_reruns_on_sixteen_gib_preserves_counts(self) -> None:
        first = self.execute()
        self.assertEqual(first.returncode, 0, first.stderr)
        (self.root / "events.jsonl").unlink()
        self.fixture["memory"] = 16 * GIB
        result = self.execute()
        self.assertEqual(result.returncode, 0, result.stderr)
        events = self.events()
        stop = next(index for index, event in enumerate(events) if event[0] == "systemctl" and "stop" in event[1])
        admission = next(index for index, event in enumerate(events) if "{{.MemTotal}}" in event[1])
        start = next(index for index, event in enumerate(events) if event[0] == "docker" and "up" in event[1])
        enable = next(index for index, event in enumerate(events) if event[0] == "systemctl" and "enable" in event[1])
        self.assertLess(stop, admission)
        self.assertLess(admission, start)
        self.assertLess(start, enable)
        arguments = events[start][1]
        self.assertIn("--no-deps", arguments)
        self.assertNotIn("bootstrap", arguments)
        self.assertIn("--wait", arguments)
        self.assertEqual([arguments[index + 1] for index, word in enumerate(arguments) if word == "--scale"],
                         ["engine=2", "web-a=3", "web-e=1"])

    def test_starter_when_memory_admission_fails_never_starts_or_enables(self) -> None:
        self.fixture["memory"] = 8 * GIB
        result = self.execute()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(any("{{.MemTotal}}" in arguments for _name, arguments in self.events()))
        self.assertFalse(any("up" in arguments or "enable" in arguments for _name, arguments in self.events()))

    def test_starter_when_compose_start_fails_never_enables(self) -> None:
        self.fixture["fail_up"] = True
        result = self.execute()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(any("up" in arguments for _name, arguments in self.events()))
        self.assertFalse(any("enable" in arguments or "--record-startup" in arguments
                             for _name, arguments in self.events()))

    def test_starter_when_bootstrap_script_changes_on_eleven_gib_refuses_overlap_without_enabling(self) -> None:
        first = self.execute()
        self.assertEqual(first.returncode, 0, first.stderr)
        (self.root / "events.jsonl").unlink()
        self.fixture["memory"] = 11 * GIB
        with (self.root / "deploy/bootstrap.py").open("a") as source:
            source.write("\nprint('changed bootstrap fixture')\n")
        result = self.execute()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("up" in arguments or "enable" in arguments for _name, arguments in self.events()))

    def test_compose_when_real_metadata_and_autoscaled_counts_match_keeps_completed_bootstrap_stopped(self) -> None:
        project = "maraclaw-starter-" + uuid.uuid4().hex[:12]
        model = {"name": project, "services": {}, "networks": {"app": {}}}
        for service in DEFAULTS:
            model["services"][service] = {"image": "maraclaw-engine:local", "mem_limit": "64m",
                "networks": ["app"], "healthcheck": {"disable": True},
                "entrypoint": ["python", "-c", "import signal,sys,time; "
                    "signal.signal(signal.SIGTERM,lambda *args:sys.exit(0)); time.sleep(300)"]}
        model["services"]["bootstrap"]["entrypoint"] = ["python", "-c", "print('bootstrap')"]
        model["services"]["engine"]["depends_on"] = {"bootstrap": {"condition": "service_completed_successfully"}}
        model["services"]["ingress"]["volumes"] = [{"type": "bind", "source": str(self.root / "state"),
                                                    "target": "/run/maraclaw-autoscaler"}]
        compose_file, env_file = self.root / "compose.json", self.root / "empty.env"
        compose_file.write_text(json.dumps(model))
        env_file.touch()
        docker = Docker(self.root, (env_file, compose_file))
        def compose(arguments: list[str]) -> None:
            subprocess.run(docker.compose + arguments, capture_output=True, text=True, check=True, timeout=60)
        try:
            docker.startup(Policy())
            compose(["up", "-d", "--no-build", "--pull", "never"])
            docker.remember_startup()
            docker.authority.save(Authority(docker.authority.settled().counts | {"web-a": 3}))
            docker.grow("web-a", 3)
            before = docker.owned(docker.containers(), "bootstrap")[0]
            counts = docker.startup(Policy())
            self.assertTrue(docker.startup_reusable)
            self.assertEqual(counts, {"engine": 1, "web-a": 3, "web-e": 1})
            compose(["up", "-d", "--no-build", "--no-deps", "--scale", "web-a=3",
                     *(service for service in DEFAULTS if service != "bootstrap")])
            after = docker.owned(docker.containers(), "bootstrap")[0]
            self.assertEqual((after.identity, after.started, after.running),
                             (before.identity, before.started, False))
        finally:
            compose(["down", "--volumes", "--timeout", "1"])
