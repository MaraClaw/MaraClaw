import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from deploy import starter, starter_service
from deploy.starter_host import endpoint, private_directory


class HostTests(unittest.TestCase):
    def test_explicit_context_takes_precedence_over_host(self) -> None:
        with patch.dict(os.environ, {"DOCKER_CONTEXT": "desktop-linux", "DOCKER_HOST": "unix:///wrong"}), \
                patch("deploy.starter_host.run", return_value="unix:///Users/fixture/.docker/run/docker.sock\n") as run:
            self.assertEqual(endpoint(), "unix:///Users/fixture/.docker/run/docker.sock")
        self.assertEqual(run.call_args.args[0][3], "desktop-linux")

    def test_host_override_without_context_is_used_without_context_lookup(self) -> None:
        with patch.dict(os.environ, {"DOCKER_HOST": "unix:///fixture/socket"}, clear=True), \
                patch("deploy.starter_host.run") as run:
            self.assertEqual(endpoint(), "unix:///fixture/socket")
        run.assert_not_called()

    def test_remote_endpoint_is_refused(self) -> None:
        with patch.dict(os.environ, {"DOCKER_HOST": "ssh://remote"}, clear=True), self.assertRaises(ValueError):
            endpoint()

    def test_world_readable_or_symlink_state_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state"
            path.mkdir(mode=0o755)
            with self.assertRaises(ValueError):
                private_directory(path)
            link = Path(temporary) / "link"
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                private_directory(link)

    def test_launchagent_uses_temporary_home_and_stub_launchctl_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            state = home / "state"
            state.mkdir(mode=0o700)
            binary = home / "launchctl"
            binary.write_text(f"#!{sys.executable}\nimport pathlib,sys\n"
                              f"pathlib.Path({str(home / 'events')!r}).write_text(repr(sys.argv[1:]))\n"
                              "sys.exit(1 if sys.argv[1] == 'print' else 0)\n")
            binary.chmod(0o700)
            command = [sys.executable, "-m", "deploy.autoscale", "--root", "/fixture/repo",
                       "--state-dir", str(state),
                       "--compose-file", "/fixture/base.yaml", "--compose-file", "/fixture/dev.json",
                       "--docker-host", "unix:///fixture/docker.sock", "--docker-binary", "/fixture/docker"]
            with patch.dict(os.environ, {"HOME": temporary, "PATH": temporary + ":" + os.environ["PATH"],
                                         "DOCKER_HOST": "unix:///fixture/docker.sock", "SECRET_KEY": "never-in-plist"}), \
                    patch.object(starter_service.os, "uname", return_value=Mock(sysname="Darwin")):
                starter_service.install(command, Path("/fixture/repo"), state)
            path = home / "Library/LaunchAgents/com.maraclaw.autoscaler.plist"
            model = plistlib.loads(path.read_bytes())
            self.assertEqual(model["ProgramArguments"], command)
            self.assertEqual(model["WorkingDirectory"], "/fixture/repo")
            self.assertEqual(model["ThrottleInterval"], 30)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(b"never-in-plist", path.read_bytes())
            self.assertIn("bootstrap", (home / "events").read_text())


class StartupSequenceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name) / "state"
        self.docker = Mock(project="isolated", compose=["compose"], startup_reusable=False,
                           images={"engine": "fixture"}, services={"engine": Mock()}, state_dir=self.state)
        self.docker.startup.return_value = {"engine": 1, "web-a": 1, "web-e": 1}
        self.events: list[str] = []
        self.docker.startup.side_effect = lambda policy: self.events.append("admit") or {"engine": 1}
        self.docker.remember_startup.side_effect = lambda: self.events.append("record")

    def invoke(self, options: list[str]) -> int:
        args = ["starter", "--state-dir", str(self.state), *options]
        with patch.object(sys, "argv", args), patch.object(starter, "prerequisites"), \
                patch.object(starter.os, "uname", return_value=Mock(sysname="Darwin")), \
                patch.dict(os.environ, {"DOCKER_HOST": "unix:///fixture/socket", "MARACLAW_DOCKER_BINARY": "/fixture/docker"}), \
                patch.object(starter, "Docker", return_value=self.docker), \
                patch.object(starter, "execute", side_effect=lambda command, timeout: self.events.append(command[1])), \
                patch.object(starter_service, "stop", side_effect=lambda *args: self.events.append("stop")), \
                patch.object(starter_service, "install", side_effect=lambda *args: self.events.append("install")), \
                patch.object(starter.os, "execv", side_effect=lambda *args: self.events.append("foreground")):
            return starter.main()

    def test_success_builds_admits_starts_records_then_installs(self) -> None:
        self.assertEqual(self.invoke([]), 0)
        self.assertEqual(self.events, ["stop", "build", "admit", "up", "record", "install"])

    def test_no_install_does_not_stop_or_install_services(self) -> None:
        self.assertEqual(self.invoke(["--no-install"]), 0)
        self.assertEqual(self.events, ["build", "admit", "up", "record"])

    def test_foreground_uses_no_host_service_operations(self) -> None:
        self.assertEqual(self.invoke(["--foreground", "--no-install"]), 0)
        self.assertEqual(self.events, ["build", "admit", "up", "record", "foreground"])

    def test_failed_admission_never_starts_records_or_installs(self) -> None:
        self.docker.startup.side_effect = ValueError("admission refused")
        self.assertEqual(self.invoke([]), 1)
        self.assertEqual(self.events, ["stop", "build"])

    def test_failed_record_never_installs_service(self) -> None:
        self.docker.remember_startup.side_effect = ValueError("bootstrap failed")
        self.assertEqual(self.invoke([]), 1)
        self.assertEqual(self.events, ["stop", "build", "admit", "up"])
