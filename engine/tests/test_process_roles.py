import os
import subprocess
import sys
from pathlib import Path

import pytest

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
