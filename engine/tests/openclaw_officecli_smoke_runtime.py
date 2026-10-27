from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from uuid import uuid4

from openclaw_officecli_smoke_cleanup import CLEANUP_PROBE, CleanupFailure, run_cleanup

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
STATE_DIR: Final = "/home/node/.openclaw"
KEYRING_PATH: Final = "/run/secrets/gogcli_keyring_password"


@dataclass(frozen=True, slots=True)
class CommandResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    def describe(self) -> str:
        return (
            f"command: {shlex.join(self.command)}\nreturn code: {self.returncode}\n"
            f"stdout:\n{self.stdout}\nstderr:\n{self.stderr}"
        )


@dataclass(frozen=True, slots=True)
class RunRequest:
    architecture: str
    scenario: str
    state_dir: Path
    offline: bool


@dataclass(frozen=True, slots=True)
class SmokeWorkspace:
    root: Path
    identifier: str
    keyring_file: Path

    @property
    def builder(self) -> str:
        return f"officecli-smoke-{self.identifier}"

    def image(self, architecture: str) -> str:
        return f"maraclaw-officecli-smoke:{self.identifier}-{architecture}"

    def container(self, architecture: str, scenario: str) -> str:
        return f"maraclaw-officecli-smoke-{self.identifier}-{architecture}-{scenario}"

    def cleanup(self) -> tuple[CleanupFailure, ...]:
        operations = []
        for architecture in ("amd64", "arm64"):
            operations.extend(
                (
                    f"permission normalization: {architecture}/{scenario}",
                    lambda architecture=architecture, scenario=scenario: assert_success(
                        "permission normalization",
                        run_container(
                            self,
                            RunRequest(architecture, f"cleanup-{scenario}", self.root / architecture / scenario, True),
                            probe=CLEANUP_PROBE,
                        ),
                    ),
                )
                for scenario in ("state", "empty")
            )
            for scenario in ("foreign", "incompatible", "online", "offline", "empty", "cleanup-state", "cleanup-empty"):
                name = self.container(architecture, scenario)
                operations.append((f"container removal: {name}", lambda name=name: _remove_container(name)))
            operations.append(
                (
                    f"image removal: {architecture}",
                    lambda architecture=architecture: assert_success(
                        "image removal", run_command(("docker", "image", "rm", "-f", self.image(architecture)))
                    ),
                )
            )
        operations.extend(
            (
                (
                    "builder removal",
                    lambda: assert_success("builder removal", run_command(("docker", "buildx", "rm", self.builder))),
                ),
                ("temporary state deletion", lambda: shutil.rmtree(self.root)),
            )
        )
        return run_cleanup(tuple(operations))


def run_command(command: tuple[str, ...], *, timeout_seconds: int = 1_200) -> CommandResult:
    try:
        completed = subprocess.run(  # noqa: S603 - real Docker integration surface, never registry publication.
            command, cwd=REPO_ROOT, capture_output=True, check=False, text=True, timeout=timeout_seconds
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        raise AssertionError(f"Docker command could not complete: {shlex.join(command)}: {error}") from error
    result = CommandResult(command, completed.returncode, completed.stdout, completed.stderr)
    if directory := os.environ.get("OPENCLAW_SMOKE_EVIDENCE_DIR", ""):
        Path(directory, f"{uuid4().hex}.log").write_text(result.describe(), encoding="utf-8")
    return result


def assert_success(label: str, result: CommandResult) -> None:
    assert result.returncode == 0, f"{label} failed\n{result.describe()}"


def _remove_container(name: str) -> None:
    existing = run_command(("docker", "container", "ls", "-aq", "--filter", f"name=^/{name}$"))
    assert_success("owned container lookup", existing)
    if existing.stdout.strip():
        assert_success("owned container removal", run_command(("docker", "rm", "-f", name)))


def create_workspace() -> SmokeWorkspace:
    root = Path(tempfile.mkdtemp(prefix="maraclaw-officecli-smoke-"))
    root.chmod(0o755)
    for architecture in ("amd64", "arm64"):
        parent = root / architecture
        parent.mkdir(mode=0o755)
        for scenario in ("state", "empty"):
            path = parent / scenario
            path.mkdir(mode=0o777)
            path.chmod(0o777)
            for managed_path in (path / ".officecli", path / ".officecli" / "releases", path / "skills"):
                managed_path.mkdir(mode=0o777)
                managed_path.chmod(0o777)
    keyring_file = root / "gogcli-keyring-password"
    keyring_file.write_text("smoke-test-keyring-password", encoding="utf-8")
    keyring_file.chmod(0o644)
    return SmokeWorkspace(root, uuid4().hex, keyring_file)


def build_image(workspace: SmokeWorkspace, architecture: str) -> CommandResult:
    return run_command(
        (
            "docker",
            "buildx",
            "build",
            "--builder",
            workspace.builder,
            "--platform",
            f"linux/{architecture}",
            "--load",
            "-f",
            "Dockerfile.openclaw",
            "-t",
            workspace.image(architecture),
            ".",
        )
    )


def run_container(workspace: SmokeWorkspace, request: RunRequest, *, probe: str) -> CommandResult:
    network = ("--network", "none") if request.offline else ()
    node_architecture = "x64" if request.architecture == "amd64" else "arm64"
    return run_command(
        (
            "docker",
            "run",
            "--name",
            workspace.container(request.architecture, request.scenario),
            "--platform",
            f"linux/{request.architecture}",
            *network,
            "--mount",
            f"type=bind,source={request.state_dir},target={STATE_DIR}",
            "--mount",
            f"type=bind,source={workspace.keyring_file},target={KEYRING_PATH},readonly",
            "-e",
            f"OPENCLAW_STATE_DIR={STATE_DIR}",
            "-e",
            "OPENCLAW_HOME=/home/node",
            "-e",
            f"GOG_HOME={STATE_DIR}/gogcli",
            "-e",
            f"GOG_KEYRING_PASSWORD_FILE={KEYRING_PATH}",
            "-e",
            "GOG_KEYRING_BACKEND=file",
            "-e",
            f"EXPECTED_NODE_ARCH={node_architecture}",
            "--entrypoint",
            "tini",
            workspace.image(request.architecture),
            "-s",
            "--",
            "/usr/local/bin/bootstrap-officecli.sh",
            "/usr/local/bin/validate-gogcli.sh",
            "sh",
            "-c",
            probe,
        )
    )
