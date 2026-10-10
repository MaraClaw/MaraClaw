"""Metadata permission checks must use inode modes, not mount access answers."""

import subprocess
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "docker/openclaw/bootstrap-officecli.sh"


@pytest.mark.parametrize("mode", [0o600, 0o700, 0o610, 0o601])
def test_metadata_mode_controls_acceptance_when_mount_reports_executable(tmp_path: Path, mode: int) -> None:
    # Given: Docker Desktop can report X_OK even for metadata with mode 0600.
    metadata = tmp_path / "metadata"
    metadata.write_text(f"tag=v1.0.144\nbinary_sha256={'a' * 64}\nskill_sha256={'b' * 64}\n", encoding="utf-8")
    metadata.chmod(mode)
    definitions = SCRIPT_PATH.read_text(encoding="utf-8").split("\nSTATE_DIR=", 1)[0].replace("[ ", "mount_test ")
    probe = tmp_path / "probe.sh"
    probe.write_text(
        definitions
        + """
mount_test() {
    case "$*" in
        '! -x '*) return 1 ;;
        '-x '*) return 0 ;;
        *) command [ "$@" ;;
    esac
}
parse_metadata "$1"
""",
        encoding="utf-8",
    )

    # When
    result = subprocess.run(  # noqa: S603 - fixed shell runs local permission probe.
        ["/bin/sh", str(probe), str(metadata)], capture_output=True, text=True, check=False
    )

    # Then: every actual execute bit is rejected, while mode 0600 is accepted.
    assert result.returncode == (0 if mode == 0o600 else 1), result.stderr
