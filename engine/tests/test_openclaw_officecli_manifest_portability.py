from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Final

import pytest

from test_openclaw_officecli_bootstrap import (
    WRAPPER_SOURCE,
    BootstrapFixture,
    _write_executable,
    create_fake_network_fixture,
)

VALID_TAG: Final = "v1.2.3"
VALID_DIGEST: Final = "a" * 64


def _install_interval_incompatible_awk(fixture: BootstrapFixture) -> None:
    real_awk = shutil.which("awk")
    assert real_awk is not None
    _write_executable(
        fixture.bin_dir / "awk",
        f"""#!/usr/bin/env python3
import os
import sys

if any("{{64}}" in argument for argument in sys.argv[1:]):
    raise SystemExit(65)
os.execv({real_awk!r}, [{real_awk!r}, *sys.argv[1:]])
""",
    )


def _metadata(*, tag: str = VALID_TAG, binary_digest: str = VALID_DIGEST, skill_digest: str = VALID_DIGEST) -> str:
    return f"tag={tag}\nbinary_sha256={binary_digest}\nskill_sha256={skill_digest}\n"


def _run_parse_metadata(tmp_path: Path, metadata: str) -> subprocess.CompletedProcess[str]:
    functions, separator, _ = WRAPPER_SOURCE.read_text(encoding="utf-8").partition("\nvalidate_release() {")
    assert separator
    metadata_file = tmp_path / "metadata"
    driver = tmp_path / "parse-metadata.sh"
    metadata_file.write_text(metadata, encoding="utf-8")
    driver.write_text(
        f'{functions}\nif ! parse_metadata "$1"; then\n    exit 1\nfi\n',
        encoding="utf-8",
    )
    return subprocess.run(  # noqa: S603 - controlled local parser driver.
        ["/bin/sh", str(driver), str(metadata_file)],
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("filename", "uppercase_digest"),
    [
        ("officecli-linux-arm64", False),
        ("*officecli-linux-arm64", False),
        ("officecli-linux-arm64", True),
    ],
    ids=("exact", "starred-exact", "uppercase-exact"),
)
def test_exact_manifest_installs_when_awk_rejects_interval_syntax(
    tmp_path: Path, filename: str, uppercase_digest: bool
) -> None:
    # Given
    fixture = create_fake_network_fixture(tmp_path)
    fixture.configure_release(VALID_TAG, "portable")
    digest = fixture.fixture_dir.joinpath("SHA256SUMS").read_text(encoding="utf-8").split()[0]
    fixture.fixture_dir.joinpath("SHA256SUMS").write_text(
        f"{digest.upper() if uppercase_digest else digest}  {filename}\n",
        encoding="utf-8",
    )
    _install_interval_incompatible_awk(fixture)

    # When
    result = fixture.run("sh", "-c", 'printf child > "$CHILD_SENTINEL"')

    # Then
    assert fixture.tencent_sentinel.exists(), result.stderr
    assert fixture.child_sentinel.exists(), result.stderr
    assert result.returncode == 0, result.stderr
    assert fixture.state_dir.joinpath(".officecli", "current").is_symlink()
    assert fixture.state_dir.joinpath("skills", "officecli").is_symlink()


@pytest.mark.parametrize(
    "manifest",
    [
        "g" * 64 + "  officecli-linux-arm64\n",
        "a" * 63 + "  officecli-linux-arm64\n",
        "a" * 64 + "  officecli-linux-arm64.exe\n",
        "a" * 64 + "  another-file\n",
        "a" * 64 + "  officecli-linux-arm64\n" + "b" * 64 + "  *officecli-linux-arm64\n",
    ],
    ids=("nonhex", "short", "near-name", "missing", "duplicate"),
)
def test_malformed_near_missing_or_duplicate_manifest_skips_officecli(tmp_path: Path, manifest: str) -> None:
    # Given
    fixture = create_fake_network_fixture(tmp_path)
    fixture.configure_release(VALID_TAG, "rejected")
    fixture.fixture_dir.joinpath("SHA256SUMS").write_text(manifest, encoding="utf-8")

    # When
    result = fixture.run("sh", "-c", ":")

    # Then
    assert result.returncode == 0, result.stderr
    assert fixture.tencent_sentinel.read_text(encoding="utf-8") == "entered"
    assert not (fixture.state_dir / ".officecli" / "current").exists()


@pytest.mark.parametrize(
    "metadata",
    [
        _metadata(tag="v1.2"),
        _metadata(binary_digest="g" * 64),
        _metadata(skill_digest="g" * 64),
    ],
    ids=("tag", "binary-digest", "skill-digest"),
)
def test_parse_metadata_propagates_each_validator_failure_from_negated_context(tmp_path: Path, metadata: str) -> None:
    # Given
    # The driver calls parse_metadata through the same negated conditional context as refresh_and_activate.

    # When
    result = _run_parse_metadata(tmp_path, metadata)

    # Then
    assert result.returncode != 0
