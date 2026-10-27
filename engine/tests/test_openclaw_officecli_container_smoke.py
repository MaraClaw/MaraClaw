from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from openclaw_officecli_smoke_cleanup import cleanup_error_message
from openclaw_officecli_smoke_probe import (
    CHILD_MARKER,
    EMPTY_OFFLINE_PROBE,
    GOG_MARKER,
    INCOMPATIBLE_OFFLINE_PROBE,
    MEMORY_MARKER,
    PROBE,
)
from openclaw_officecli_smoke_runtime import (
    RunRequest,
    SmokeWorkspace,
    assert_success,
    build_image,
    create_workspace,
    run_command,
    run_container,
)


@pytest.fixture(scope="module")
def smoke_workspace() -> Iterator[SmokeWorkspace]:
    if os.environ.get("RUN_OPENCLAW_OFFICECLI_SMOKE") != "1":
        pytest.skip("set RUN_OPENCLAW_OFFICECLI_SMOKE=1 to execute both real Docker guest targets")
    assert_success("host Node pin", run_command(("node", "--version")))
    assert run_command(("node", "--version")).stdout.strip() == "v26.12.1"
    assert_success("Docker availability", run_command(("docker", "version")))
    workspace = create_workspace()
    try:
        assert_success(
            "isolated builder creation",
            run_command(("docker", "buildx", "create", "--name", workspace.builder, "--driver", "docker-container")),
        )
        for architecture in ("amd64", "arm64"):
            assert_success(f"linux/{architecture} image build", build_image(workspace, architecture))
        yield workspace
    finally:
        failures = workspace.cleanup()
        if (message := cleanup_error_message(failures, sys.exception())) not in (None, ""):
            pytest.fail(message)


def _assert_online_state(state_dir: Path, architecture: str) -> str:
    current = state_dir / ".officecli" / "current"
    assert current.is_symlink()
    assert str(current.readlink()) == f"releases/v1.0.158-linux-{architecture}"
    for path in (current / "officecli", current / "skill" / "SKILL.md"):
        assert path.is_file()
        assert not path.is_symlink()
    assert os.access(current / "officecli", os.X_OK)
    assert f"platform=linux/{architecture}\n" in current.joinpath("metadata").read_text()
    assert (state_dir / GOG_MARKER).is_file()
    assert (state_dir / CHILD_MARKER).is_file()
    assert not list(state_dir.joinpath(".officecli").glob(".staging.*"))
    assert not list(state_dir.joinpath(".officecli").glob(".current.*"))
    return str(current.readlink())


@pytest.mark.parametrize("architecture", ["amd64", "arm64"])
def test_real_dual_platform_officecli_restart_smoke(smoke_workspace: SmokeWorkspace, architecture: str) -> None:
    # Given
    workspace = smoke_workspace
    state = workspace.root / architecture / "state"
    empty_state = workspace.root / architecture / "empty"
    other = "arm64" if architecture == "amd64" else "amd64"
    foreign = run_container(workspace, RunRequest(other, "foreign", state, False), probe=PROBE)
    assert_success("foreign target online install", foreign)
    _assert_online_state(state, other)

    # When
    incompatible = run_container(
        workspace, RunRequest(architecture, "incompatible", state, True), probe=INCOMPATIBLE_OFFLINE_PROBE
    )

    # Then
    assert_success("incompatible offline cache rejection", incompatible)
    assert "OfficeCLI unavailable" in incompatible.stderr
    assert not list(state.joinpath(".officecli").glob(".staging.*"))
    online = run_container(workspace, RunRequest(architecture, "online", state, False), probe=PROBE)
    assert_success("online target install", online)
    original_current = _assert_online_state(state, architecture)
    assert state.joinpath(".officecli", "releases", f"v1.0.158-linux-{other}").is_dir()
    for marker in (GOG_MARKER, CHILD_MARKER):
        state.joinpath(marker).unlink()
    offline = run_container(workspace, RunRequest(architecture, "offline", state, True), probe=PROBE)
    assert_success("validated same-target offline restart", offline)
    assert "curl:" in offline.stderr
    assert _assert_online_state(state, architecture) == original_current
    empty = run_container(workspace, RunRequest(architecture, "empty", empty_state, True), probe=EMPTY_OFFLINE_PROBE)
    assert_success("empty offline startup", empty)
    for marker in (MEMORY_MARKER, GOG_MARKER, CHILD_MARKER):
        assert (empty_state / marker).is_file()
    server_arch = run_command(("docker", "info", "--format", "{{.Architecture}}"))
    assert_success("Docker host architecture", server_arch)
    native = {"amd64": ("x86_64", "amd64"), "arm64": ("aarch64", "arm64")}
    execution = "native" if server_arch.stdout.strip() in native[architecture] else "emulated"
    sys.stdout.write(
        f"linux/{architecture}: {execution} execution on {server_arch.stdout.strip()}\n{online.stdout}\n{offline.stdout}\n{empty.stdout}\n"
    )
