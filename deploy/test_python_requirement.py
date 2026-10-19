import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Final
import unittest
from unittest.mock import call, patch

from deploy import starter_host


ROOT: Final = Path(__file__).resolve().parents[1]


class PythonRequirementTests(unittest.TestCase):
    def test_prerequisites_when_python_is_older_rejects_before_docker_access(self) -> None:
        for minor in (11, 12, 13):
            for darwin in (False, True):
                with self.subTest(minor=minor, darwin=darwin), \
                        patch.object(starter_host.sys, "version_info", (3, minor, 0)), \
                        patch.dict(os.environ, {"DOCKER_HOST": "unix:///var/run/docker.sock"}, clear=True), \
                        patch.object(starter_host.shutil, "which", return_value="/fixture/docker") as discovery, \
                        patch.object(starter_host, "run", side_effect=["2.24.0\n", "linux\n"]) as docker:
                    with self.assertRaises(ValueError):
                        starter_host.prerequisites(darwin)
                    discovery.assert_not_called()
                    docker.assert_not_called()

    def test_prerequisites_when_python_is_supported_completes_docker_checks(self) -> None:
        for minor in (14, 15):
            for darwin in (False, True):
                with self.subTest(minor=minor, darwin=darwin), \
                        patch.object(starter_host.sys, "version_info", (3, minor, 0)), \
                        patch.dict(os.environ, {"DOCKER_HOST": "unix:///var/run/docker.sock"}, clear=True), \
                        patch.object(starter_host.shutil, "which", return_value="/fixture/docker"), \
                        patch.object(starter_host, "run", side_effect=["2.24.0\n", "linux\n"]) as docker:
                    starter_host.prerequisites(darwin)

                    self.assertEqual(docker.call_args_list, [
                        call(["docker", "compose", "version", "--short"]),
                        call(["docker", "info", "--format", "{{.OSType}}"]),
                    ])

    def test_linux_shell_version_command_when_python_crosses_minimum(self) -> None:
        command = next(line for line in (ROOT / "start-docker-compose.sh").read_text().splitlines()
                       if "sys.version_info" in line)
        with tempfile.TemporaryDirectory() as temporary:
            python = Path(temporary) / "python3"
            environment = dict(os.environ, PATH=temporary + os.pathsep + os.environ["PATH"])
            for minor, expected_status in ((11, 1), (12, 1), (13, 1), (14, 0), (15, 0)):
                with self.subTest(minor=minor):
                    python.write_text(f"#!{sys.executable}\nimport sys\n"
                                      f"sys.version_info = (3, {minor}, 0)\nexec(sys.argv[2])\n")
                    python.chmod(0o700)

                    result = subprocess.run(["bash", "-c", command], env=environment,
                                            capture_output=True, text=True, timeout=35, check=False)

                    self.assertEqual(result.returncode, expected_status, result.stderr)
