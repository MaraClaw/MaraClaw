from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from uuid import uuid4

import pytest

from openclaw_officecli_smoke_cleanup import CLEANUP_PROBE, CleanupFailure, cleanup_error_message, run_cleanup
from openclaw_officecli_smoke_probe import CHILD_MARKER, GOG_MARKER, PROBE, TENCENT_MARKER

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
SMOKE_FLAG: Final = "RUN_OPENCLAW_OFFICECLI_SMOKE"
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
            f"command: {shlex.join(self.command)}\n"
            f"return code: {self.returncode}\n"
            f"stdout:\n{self.stdout}\n"
            f"stderr:\n{self.stderr}"
        )


@dataclass(frozen=True, slots=True)
class RunRequest:
    name: str
    state_dir: Path
    offline: bool


@dataclass(frozen=True, slots=True)
class SmokeWorkspace:
    root: Path
    image_tag: str
    state_dir: Path
    empty_state_dir: Path
    keyring_file: Path
    container_names: tuple[str, ...]

    def cleanup(self) -> tuple[CleanupFailure, ...]:
        return run_cleanup(
            (
                (
                    "permission normalization",
                    lambda: _cleanup_result(
                        _run_container(
                            self,
                            RunRequest(self.container_names[-1], self.state_dir, offline=True),
                            probe=CLEANUP_PROBE,
                        )
                    ),
                ),
                *(
                    (
                        f"container removal: {name}",
                        lambda name=name: _cleanup_result(_cleanup_command(("docker", "rm", "-f", name))),
                    )
                    for name in self.container_names
                ),
                (
                    "image removal",
                    lambda: _cleanup_result(_cleanup_command(("docker", "image", "rm", "-f", self.image_tag))),
                ),
                ("temporary state deletion", lambda: shutil.rmtree(self.root)),
            )
        )


def _run_command(command: tuple[str, ...], *, timeout_seconds: int = 1_200) -> CommandResult:
    try:
        completed = subprocess.run(  # noqa: S603 - this integration test intentionally invokes the Docker CLI.
            command,
            cwd=REPO_ROOT,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout_seconds,
        )
    except FileNotFoundError as error:
        raise AssertionError(
            f"Docker executable is unavailable. Install and start Docker, then retry: {error}"
        ) from error
    except subprocess.TimeoutExpired as error:
        raise AssertionError(
            f"Docker command timed out after {timeout_seconds} seconds: {shlex.join(command)}\n"
            f"stdout:\n{error.stdout}\n"
            f"stderr:\n{error.stderr}"
        ) from error
    return CommandResult(command, completed.returncode, completed.stdout, completed.stderr)


def _cleanup_command(command: tuple[str, ...]) -> CommandResult:
    return _run_command(command, timeout_seconds=90)


def _cleanup_result(result: CommandResult) -> None:
    if result.returncode != 0:
        raise AssertionError(result.describe())


def _assert_success(label: str, result: CommandResult) -> None:
    assert result.returncode == 0, f"{label} failed\n{result.describe()}"


def _require_docker_buildx() -> None:
    docker_path = _run_command(("sh", "-c", "command -v docker"), timeout_seconds=30)
    _assert_success("Docker executable path check", docker_path)
    docker_version = _run_command(("docker", "version", "--format", "{{.Server.Version}}"), timeout_seconds=30)
    buildx_version = _run_command(("docker", "buildx", "version"), timeout_seconds=30)
    buildx_inspect = _run_command(("docker", "buildx", "inspect", "--bootstrap"), timeout_seconds=90)
    _assert_success("Docker daemon availability check", docker_version)
    _assert_success("Docker buildx availability check", buildx_version)
    _assert_success("Docker buildx bootstrap check", buildx_inspect)


def _create_workspace() -> SmokeWorkspace:
    root = Path(tempfile.mkdtemp(prefix="maraclaw-officecli-smoke-"))
    root.chmod(0o755)
    state_dir, empty_state_dir = root / "state", root / "empty-state"
    for path in (state_dir, empty_state_dir):
        path.mkdir(mode=0o777)
        path.chmod(0o777)
        for managed_path in (path / ".officecli", path / ".officecli" / "releases", path / "skills"):
            managed_path.mkdir(mode=0o777)
            managed_path.chmod(0o777)
    keyring_file = root / "gogcli-keyring-password"
    keyring_file.write_text("smoke-test-keyring-password", encoding="utf-8")
    keyring_file.chmod(0o644)
    identifier = uuid4().hex
    return SmokeWorkspace(
        root=root,
        image_tag=f"maraclaw-officecli-smoke:{identifier}",
        state_dir=state_dir,
        empty_state_dir=empty_state_dir,
        keyring_file=keyring_file,
        container_names=tuple(
            f"maraclaw-officecli-smoke-{identifier}-{scenario}"
            for scenario in ("online", "offline", "empty", "cleanup")
        ),
    )


def _build_image(workspace: SmokeWorkspace) -> CommandResult:
    return _run_command(
        (
            "docker",
            "buildx",
            "build",
            "--platform",
            "linux/arm64",
            "--load",
            "-f",
            "Dockerfile.openclaw",
            "-t",
            workspace.image_tag,
            ".",
        )
    )


def _run_container(workspace: SmokeWorkspace, request: RunRequest, *, probe: str = PROBE) -> CommandResult:
    network = ("--network", "none") if request.offline else ()
    return _run_command(
        (
            "docker",
            "run",
            "--name",
            request.name,
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
            "OPENCLAW_MEMORY_TENCENTDB_ENABLED=true",
            workspace.image_tag,
            "/usr/local/bin/validate-gogcli.sh",
            "sh",
            "-c",
            probe,
        )
    )


def _assert_online_state(state_dir: Path) -> str:
    current = state_dir / ".officecli" / "current"
    binary = current / "officecli"
    skill = current / "skill" / "SKILL.md"
    assert current.is_symlink()
    assert binary.is_file()
    assert not binary.is_symlink()
    assert os.access(binary, os.X_OK)
    assert skill.is_file()
    assert not skill.is_symlink()
    assert (state_dir / TENCENT_MARKER).is_file()
    assert (state_dir / GOG_MARKER).is_file()
    assert (state_dir / CHILD_MARKER).is_file()
    return os.readlink(current)


def test_real_linux_arm64_officecli_restart_smoke() -> None:
    if os.environ.get(SMOKE_FLAG) != "1":
        pytest.skip(f"set {SMOKE_FLAG}=1 to run the real Docker linux/arm64 smoke test")

    _require_docker_buildx()
    workspace = _create_workspace()
    try:
        build = _build_image(workspace)
        _assert_success("linux/arm64 image build", build)

        online = _run_container(
            workspace,
            RunRequest(workspace.container_names[0], workspace.state_dir, offline=False),
        )
        _assert_success("online first start", online)
        original_current = _assert_online_state(workspace.state_dir)

        gog_marker = workspace.state_dir / GOG_MARKER
        child_marker = workspace.state_dir / CHILD_MARKER
        gog_marker.unlink()
        child_marker.unlink()
        offline = _run_container(
            workspace,
            RunRequest(workspace.container_names[1], workspace.state_dir, offline=True),
        )
        _assert_success("offline LKG restart", offline)
        assert "curl:" in f"{offline.stdout}\n{offline.stderr}", offline.describe()
        assert _assert_online_state(workspace.state_dir) == original_current

        empty = _run_container(
            workspace,
            RunRequest(workspace.container_names[2], workspace.empty_state_dir, offline=True),
        )
        assert empty.returncode == 0, f"empty-state offline start failed\n{empty.describe()}"
        assert (workspace.empty_state_dir / TENCENT_MARKER).is_file()
        assert (workspace.empty_state_dir / GOG_MARKER).is_file()
        assert (workspace.empty_state_dir / CHILD_MARKER).is_file()

    finally:
        if message := cleanup_error_message(workspace.cleanup(), sys.exception()):
            pytest.fail(message)
