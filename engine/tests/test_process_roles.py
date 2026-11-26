import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.config import Settings
from app.process_roles import InvalidProcessRoleError, parse_process_roles


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", {"all"}),
        (" , ", {"all"}),
        (" Bootstrap , API,bootstrap ", {"bootstrap", "api"}),
        (" ALL , worker", {"all"}),
        ("Connector", {"connector"}),
    ],
)
def test_roles_normalize_and_shell_matches(raw: str, expected: set[str]) -> None:
    # Given
    env = dict(os.environ, PROCESS_ROLE=raw)
    # When
    result = subprocess.run([sys.executable, "-S", "-m", "app.process_roles"], env=env, capture_output=True, text=True)
    # Then
    assert result.returncode == 0
    assert set(result.stdout.strip().split(",")) == expected == parse_process_roles(raw)


def test_unknown_role_rejected_before_entrypoint_permissions(tmp_path: Path) -> None:
    # Given
    commands = tmp_path / "bin"
    commands.mkdir()
    marker = tmp_path / "permissions"
    python = commands / "python"
    python.symlink_to(sys.executable)
    fake_id = commands / "id"
    fake_id.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 0\n')
    fake_id.chmod(0o755)
    env = dict(os.environ, PROCESS_ROLE="all,wat", PATH=f"{commands}:{os.environ['PATH']}")
    # When
    result = subprocess.run(["/bin/bash", "entrypoint.sh"], env=env, capture_output=True, text=True)
    # Then
    assert result.returncode != 0
    assert not marker.exists()
    with pytest.raises(InvalidProcessRoleError):
        parse_process_roles("all,wat")


@pytest.mark.parametrize(("raw", "bootstrap"), [(" Bootstrap , API ", True), ("WORKER,connector", False), ("", True)])
def test_entrypoint_normalized_bootstrap_decision(tmp_path: Path, raw: str, bootstrap: bool) -> None:
    # Given
    marker = tmp_path / "bootstrapped"
    python = tmp_path / "python"
    python.write_text(
        f'#!/bin/sh\nif [ "$2" = app.scripts.bootstrap_db ]; then touch "{marker}"; exit 0; fi\n'
        f'exec "{sys.executable}" -S "$@"\n'
    )
    python.chmod(0o755)
    fake_id = tmp_path / "id"
    fake_id.write_text("#!/bin/sh\nprintf 1000\n")
    fake_id.chmod(0o755)
    env = dict(
        os.environ,
        PROCESS_ROLE=raw,
        PATH=f"{tmp_path}:{os.environ['PATH']}",
        INSTANCE_ID="test-instance",
        START_COMMAND="true",
    )
    # When
    result = subprocess.run(["/bin/bash", "entrypoint.sh"], env=env, capture_output=True, text=True)
    # Then
    assert result.returncode == 0, result.stderr
    assert marker.exists() == bootstrap


@pytest.mark.parametrize(
    ("preset", "window"),
    [
        ({}, str(Settings.model_fields["SHUTDOWN_HTTP_GRACE_SECONDS"].default)),
        ({"SHUTDOWN_HTTP_GRACE_SECONDS": "45"}, "45"),
        ({"SHUTDOWN_HTTP_GRACE_SECONDS": "45", "UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN": "7"}, "7"),
    ],
    ids=["default window", "window setting", "preset uvicorn variable"],
)
def test_entrypoint_execs_the_start_command_in_its_own_process(
    tmp_path: Path, preset: dict[str, str], window: str
) -> None:
    # Given a start command that prints its own pid and the uvicorn shutdown window it inherited.
    python = tmp_path / "python"
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" -S "$@"\n')
    python.chmod(0o755)
    fake_id = tmp_path / "id"
    fake_id.write_text("#!/bin/sh\nprintf 1000\n")
    fake_id.chmod(0o755)
    inherited = {
        key: value
        for key, value in os.environ.items()
        if key not in {"SHUTDOWN_HTTP_GRACE_SECONDS", "UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN"}
    }
    env = {
        **inherited,
        **preset,
        "PROCESS_ROLE": "api",
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "INSTANCE_ID": "test-instance",
        "START_COMMAND": """python -c 'import os; print(os.getpid(), os.environ["UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN"])'""",
    }
    # When the image's entrypoint launches it.
    process = subprocess.Popen(
        ["/bin/bash", "entrypoint.sh"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    stdout, stderr = process.communicate(timeout=60)
    # Then the server is the launched process itself, so docker stop's SIGTERM reaches it, and it got the window.
    assert process.returncode == 0, stderr
    assert stdout.splitlines()[-1].split() == [str(process.pid), window]


def _entrypoint_run(tmp_path: Path, start_command: str | None) -> list[str]:
    """Run the entrypoint with a uvicorn that prints its arguments; python is the real interpreter (settings load)."""
    python = tmp_path / "python"
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    python.chmod(0o755)
    fake_id = tmp_path / "id"
    fake_id.write_text("#!/bin/sh\nprintf 1000\n")
    fake_id.chmod(0o755)
    uvicorn = tmp_path / "uvicorn"
    uvicorn.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    uvicorn.chmod(0o755)
    env = {key: value for key, value in os.environ.items() if key != "START_COMMAND" and not key.startswith("CHAT_")}
    env |= {"PROCESS_ROLE": "api", "PATH": f"{tmp_path}:{os.environ['PATH']}", "INSTANCE_ID": "test-instance"}
    if start_command is not None:
        env["START_COMMAND"] = start_command
    result = subprocess.run(["/bin/bash", "entrypoint.sh"], env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()[-1:] if start_command is None else result.stdout.splitlines()


def test_default_start_command_bounds_the_websocket_frame_from_the_chat_limits(tmp_path: Path) -> None:
    # Given no START_COMMAND, the entrypoint launches uvicorn itself.
    output = _entrypoint_run(tmp_path, None)
    # Then the frame limit is the settings' derived value, so an oversized frame closes 1009 before parsing.
    assert output == [str(Settings().chat_frame_max_bytes())]


def test_custom_start_command_keeps_every_server_flag(tmp_path: Path) -> None:
    # Given a deployment's own START_COMMAND, the entrypoint adds nothing to it.
    assert _entrypoint_run(tmp_path, "uvicorn app.main:app --port 9000")[-3:] == ["app.main:app", "--port", "9000"]
