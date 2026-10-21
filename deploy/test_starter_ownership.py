import json
import os
from pathlib import Path
import plistlib
import shlex
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from deploy import starter, starter_service
from deploy.starter_legacy import TEMPLATE


class OwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        mask = os.umask(0o077)
        os.umask(mask)
        self.addCleanup(os.umask, mask)
        self.root = self.home / "checkout"
        self.root.mkdir()
        self.state = self.home / "state"
        self.state.mkdir(mode=0o700)
        self.endpoint = "unix:///fixture/docker.sock"
        self.command = [sys.executable, "-m", "deploy.autoscale", "--root", str(self.root),
                        "--state-dir", str(self.state), "--docker-host", self.endpoint]
        self.plist = self.home / "Library/LaunchAgents" / (starter_service.LABEL + ".plist")
        self.plist.parent.mkdir(parents=True)
        self.events = self.home / "events"
        self.loaded = self.home / "loaded"
        for name in ("launchctl", "systemctl"):
            binary = self.home / name
            binary.write_text(f"#!{sys.executable}\nimport json,pathlib,sys\n"
                              f"home=pathlib.Path({str(self.home)!r})\n"
                              "with (home/'events').open('a') as output: output.write(json.dumps(sys.argv[1:])+'\\n')\n"
                              "if sys.argv[1] in ('print','show'):\n"
                              " source=home/'loaded'\n"
                              " if not source.exists(): sys.exit(1)\n"
                              " print(source.read_text())\n")
            binary.chmod(0o700)
        environment = patch.dict(os.environ, {"HOME": str(self.home),
            "PATH": str(self.home) + ":" + os.environ["PATH"], "DOCKER_HOST": self.endpoint,
            "MARACLAW_DOCKER_BINARY": "/fixture/docker"})
        environment.start()
        self.addCleanup(environment.stop)

    def legacy(self) -> tuple[Path, dict[str, str]]:
        unit = self.home / "controller.service"
        unit.write_text(TEMPLATE.replace("@ROOT@", str(self.root)).replace("@PYTHON@", sys.executable))
        values = {"LoadState": "loaded", "FragmentPath": str(unit), "DropInPaths": "", "NeedDaemonReload": "no",
                  "WorkingDirectory": str(self.root), "User": "root", "Type": "simple",
                  "Environment": "DOCKER_HOST=unix:///var/run/docker.sock", "EnvironmentFiles": "",
                  "ExecStartPre": "", "ExecStartPost": "", "ExecStop": "", "ExecStopPost": "", "RootDirectory": "",
                  "Restart": "on-failure", "RestartUSec": "10s", "TimeoutStopUSec": "4min", "UMask": "0077",
                  "NoNewPrivileges": "yes", "PrivateTmp": "yes",
                  "ExecStart": "{ path=" + sys.executable + " ; argv[]="
                      + shlex.join([sys.executable, "-m", "deploy.autoscale", "--root", str(self.root)]) + " ; }"}
        return unit, values

    def load_unit(self, values: dict[str, str]) -> None:
        self.loaded.write_text("\n".join(key + "=" + value for key, value in values.items()))

    def test_approved_legacy_transition_installs_explicit_owner_only_after_verified_stop(self) -> None:
        unit, values = self.legacy()
        self.load_unit(values)
        state = Path("/var/lib/maraclaw-autoscaler")
        endpoint = "unix:///var/run/docker.sock"
        command = [sys.executable, "-m", "deploy.autoscale", "--root", str(self.root),
                   "--state-dir", str(state), "--docker-host", endpoint]
        (self.root / "deploy").mkdir()
        (self.root / "deploy/maraclaw-autoscaler.service").write_text(TEMPLATE)
        with patch.object(starter_service, "UNIT", unit), \
                patch.dict(os.environ, {"DOCKER_HOST": endpoint}), \
                patch.object(starter_service.os, "uname", return_value=Mock(sysname="Linux")), \
                patch.object(starter_service, "private_directory"):
            starter_service.stop(False, self.root, state, approve_legacy=True)
            starter_service.install(command, self.root, state, approve_legacy=True)
        self.assertEqual(self.operations(), ["show", "stop", "show", "daemon-reload", "enable"])
        starter_service.Ownership(self.root, state, endpoint).unit(unit)

    def persist(self, command: list[str]) -> None:
        model = {"Label": starter_service.LABEL, "ProgramArguments": command,
                 "WorkingDirectory": command[command.index("--root") + 1],
                 "EnvironmentVariables": {"DOCKER_HOST": command[command.index("--docker-host") + 1]}}
        self.plist.write_bytes(plistlib.dumps(model))

    def load(self, command: list[str]) -> None:
        self.loaded.write_text("job = {\n arguments = {\n" + "\n".join("  " + item for item in command)
            + "\n }\n working directory = " + command[command.index("--root") + 1] + "\n}\n")

    def foreign(self, flag: str) -> list[str]:
        command = list(self.command)
        command[command.index(flag) + 1] += "-other"
        return command

    def operations(self) -> list[str]:
        return [json.loads(line)[0] for line in self.events.read_text().splitlines()] if self.events.exists() else []

    def test_same_owner_can_stop_and_replace_persisted_loaded_job(self) -> None:
        self.persist(self.command)
        self.load(self.command)
        replacement = [*self.command, "--policy", str(self.home / "policy.toml")]
        with patch.object(starter_service.os, "uname", return_value=Mock(sysname="Darwin")):
            starter_service.stop(True, self.root, self.state)
            starter_service.install(replacement, self.root, self.state)
        self.assertEqual(plistlib.loads(self.plist.read_bytes())["ProgramArguments"], replacement)
        self.assertEqual(self.operations(), ["print", "bootout", "print", "bootstrap"])

    def test_foreign_loaded_or_persisted_owner_is_never_stopped_or_replaced(self) -> None:
        for flag in ("--root", "--state-dir", "--docker-host"):
            for source in ("loaded", "persisted", "loaded-only", "persisted-only"):
                with self.subTest(flag=flag, source=source):
                    self.events.unlink(missing_ok=True)
                    self.plist.unlink(missing_ok=True)
                    self.loaded.unlink(missing_ok=True)
                    if source != "loaded-only":
                        self.persist(self.foreign(flag) if source.startswith("persisted") else self.command)
                    if source != "persisted-only":
                        self.load(self.foreign(flag) if source.startswith("loaded") else self.command)
                    before = self.plist.read_bytes() if self.plist.exists() else None
                    with self.assertRaisesRegex(ValueError, "foreign"):
                        starter_service.stop(True, self.root, self.state)
                    with patch.object(starter_service.os, "uname", return_value=Mock(sysname="Darwin")), \
                            self.assertRaisesRegex(ValueError, "foreign"):
                        starter_service.install(self.command, self.root, self.state)
                    self.assertEqual(self.plist.read_bytes() if self.plist.exists() else None, before)
                    self.assertFalse({"bootout", "bootstrap"} & set(self.operations()))

    def test_foreign_loaded_job_is_untouched_when_requested_project_would_fail_admission(self) -> None:
        self.persist(self.command)
        self.load(self.foreign("--state-dir"))
        before = self.plist.read_bytes()
        docker = Mock(state_dir=self.state)
        docker.startup.side_effect = ValueError("admission refused")
        args = ["starter", "--root", str(self.root), "--state-dir", str(self.state),
                "--env-file", str(self.home / "synthetic.env")]
        with patch.object(sys, "argv", args), patch.object(starter, "prerequisites"), \
                patch.object(starter.os, "uname", return_value=Mock(sysname="Darwin")), \
                patch.object(starter, "Docker", return_value=docker):
            self.assertEqual(starter.main(), 1)
        docker.startup.assert_not_called()
        self.assertEqual(self.plist.read_bytes(), before)
        self.assertEqual(self.operations(), ["print"])

    def test_ambiguous_loaded_ownership_flags_cannot_hide_foreign_effective_owner(self) -> None:
        for override in ("--root=/foreign", "--state-d=/foreign", "--docker-h=unix:///foreign"):
            with self.subTest(override=override):
                self.events.unlink(missing_ok=True)
                self.load([*self.command, override])
                with self.assertRaisesRegex(ValueError, "ownership is unproven"):
                    starter_service.stop(True, self.root, self.state)
                self.assertEqual(self.operations(), ["print"])

    def test_python_linux_path_refuses_foreign_loaded_or_persisted_unit(self) -> None:
        unit = self.home / "controller.service"
        for flag in ("--root", "--state-dir", "--docker-host"):
            for persisted in (False, True):
                with self.subTest(flag=flag, persisted=persisted):
                    self.events.unlink(missing_ok=True)
                    command = self.foreign(flag)
                    text = "[Service]\nWorkingDirectory=" + command[command.index("--root") + 1]
                    text += "\nExecStart=" + shlex.join(command) + "\n"
                    unit.write_text(text)
                    self.loaded.write_text("LoadState=loaded\nWorkingDirectory="
                        + command[command.index("--root") + 1]
                        + "\nExecStart={ path=/python ; argv[]=" + shlex.join(command) + " ; }\n")
                    if not persisted:
                        unit.unlink()
                    before = unit.read_bytes() if unit.exists() else None
                    with patch.object(starter_service, "UNIT", unit), \
                            patch.object(starter_service.os, "uname", return_value=Mock(sysname="Linux")):
                        with self.assertRaisesRegex(ValueError, "foreign"):
                            starter_service.stop(False, self.root, self.state)
                        with self.assertRaisesRegex(ValueError, "foreign"):
                            starter_service.install(self.command, self.root, self.state)
                    self.assertEqual(unit.read_bytes() if unit.exists() else None, before)
                    self.assertFalse({"stop", "enable", "daemon-reload"} & set(self.operations()))

    def test_python_linux_path_can_replace_same_owner_unit_with_stub_systemctl(self) -> None:
        unit = self.home / "controller.service"
        text = "[Service]\nWorkingDirectory=" + str(self.root) + "\nExecStart=" + shlex.join(self.command) + "\n"
        unit.write_text(text)
        (self.root / "deploy").mkdir()
        (self.root / "deploy/maraclaw-autoscaler.service").write_text(text)
        self.loaded.write_text("LoadState=loaded\nWorkingDirectory=" + str(self.root)
            + "\nExecStart={ path=/python ; argv[]=" + shlex.join(self.command) + " ; }\n")
        replacement = [*self.command, "--policy", str(self.home / "policy.toml")]
        with patch.object(starter_service, "UNIT", unit), \
                patch.object(starter_service.os, "uname", return_value=Mock(sysname="Linux")):
            starter_service.stop(False, self.root, self.state)
            starter_service.install(replacement, self.root, self.state)
        self.assertIn("ExecStart=" + " ".join(replacement), unit.read_text())
        self.assertEqual(self.operations(), ["show", "stop", "show", "daemon-reload", "enable"])

    def test_shipped_legacy_requires_explicit_approval_before_stop_or_replace(self) -> None:
        unit, values = self.legacy()
        self.load_unit(values)
        with patch.object(starter_service, "UNIT", unit), \
                patch.dict(os.environ, {"DOCKER_HOST": "unix:///var/run/docker.sock"}), \
                self.assertRaises(ValueError):
            starter_service.stop(False, self.root, Path("/var/lib/maraclaw-autoscaler"))
        self.assertEqual(self.operations(), [])

    def test_approved_shipped_legacy_checks_installed_and_loaded_before_stop(self) -> None:
        unit, values = self.legacy()
        self.load_unit(values)
        with patch.object(starter_service, "UNIT", unit), \
                patch.dict(os.environ, {"DOCKER_HOST": "unix:///var/run/docker.sock"}):
            starter_service.stop(False, self.root, Path("/var/lib/maraclaw-autoscaler"), approve_legacy=True)
        self.assertEqual(self.operations(), ["show", "stop"])

    def test_approval_cannot_bypass_modified_or_stale_legacy_definitions(self) -> None:
        for source in ("installed", "missing-installed", "loaded-root", "loaded-exec", "loaded-path", "drop-in", "stale", "environment"):
            with self.subTest(source=source):
                self.events.unlink(missing_ok=True)
                unit, values = self.legacy()
                if source == "installed":
                    unit.write_text(unit.read_text().replace("User=root", "User=other"))
                if source == "missing-installed":
                    unit.unlink()
                if source == "loaded-root":
                    values["WorkingDirectory"] = "/foreign"
                if source == "loaded-exec":
                    values["ExecStart"] = values["ExecStart"].replace("deploy.autoscale", "foreign.module")
                if source == "loaded-path":
                    values["ExecStart"] = values["ExecStart"].replace("path=" + sys.executable, "path=/foreign/python")
                if source == "drop-in":
                    values["DropInPaths"] = "/foreign/override.conf"
                if source == "stale":
                    values["NeedDaemonReload"] = "yes"
                if source == "environment":
                    values["Environment"] += " DOCKER_CONTEXT=foreign"
                self.load_unit(values)
                before = unit.read_bytes() if unit.exists() else None
                with patch.object(starter_service, "UNIT", unit), \
                        patch.dict(os.environ, {"DOCKER_HOST": "unix:///var/run/docker.sock"}), \
                        self.assertRaises(ValueError):
                    starter_service.stop(False, self.root, Path("/var/lib/maraclaw-autoscaler"), approve_legacy=True)
                self.assertEqual(unit.read_bytes() if unit.exists() else None, before)
                self.assertFalse({"stop", "enable", "daemon-reload"} & set(self.operations()))
