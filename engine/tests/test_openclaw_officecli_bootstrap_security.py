from __future__ import annotations

import hashlib
import os
from pathlib import Path
from subprocess import CompletedProcess

import pytest

import test_openclaw_officecli_bootstrap as bootstrap_tests
from test_openclaw_officecli_bootstrap import (
    FAKE_GNU_MV,
    BootstrapFixture,
    _write_executable,
    create_fake_network_fixture,
)


def configure_fake_gnu_mv_log(fixture: BootstrapFixture, monkeypatch: pytest.MonkeyPatch, log_path: Path) -> None:
    monkeypatch.setenv("FAKE_MV_LOG", str(log_path))
    if bootstrap_tests.sys.platform != "darwin":
        _write_executable(fixture.bin_dir / "mv", FAKE_GNU_MV)


def prepare_lkg(fixture: BootstrapFixture) -> tuple[bytes, bytes, bytes, str]:
    fixture.configure_release("v1.2.3", "lkg")
    assert fixture.run("sh", "-c", ":").returncode == 0
    fixture.tencent_sentinel.unlink()
    marker = fixture.state_dir / ".bootstrap-tencentdb-version"
    marker.unlink()
    release = fixture.state_dir / ".officecli" / "releases" / "v1.2.3"
    return (
        release.joinpath("officecli").read_bytes(),
        release.joinpath("skill", "SKILL.md").read_bytes(),
        release.joinpath("metadata").read_bytes(),
        os.readlink(fixture.state_dir / ".officecli" / "current"),
    )


def assert_lkg_execution(
    fixture: BootstrapFixture, result: CompletedProcess[str], snapshot: tuple[bytes, bytes, bytes, str]
) -> None:
    release = fixture.state_dir / ".officecli" / "releases" / "v1.2.3"
    assert result.returncode == 0, result.stderr
    assert fixture.tencent_sentinel.read_text(encoding="utf-8") == "entered"
    assert fixture.child_sentinel.read_text(encoding="utf-8") == "lkg"
    assert release.joinpath("officecli").read_bytes() == snapshot[0]
    assert release.joinpath("skill", "SKILL.md").read_bytes() == snapshot[1]
    assert release.joinpath("metadata").read_bytes() == snapshot[2]
    assert os.readlink(fixture.state_dir / ".officecli" / "current") == snapshot[3]


def install_metadata_failure_chmod(fixture: BootstrapFixture) -> None:
    _write_executable(
        fixture.bin_dir / "chmod",
        """#!/usr/bin/env python3
import os
import sys
from pathlib import Path

mode, target = sys.argv[1:]
if os.environ.get("OFFICECLI_FAKE_MODE") == "metadata_fail" and mode == "0600" and target.endswith("/metadata"):
    raise SystemExit(84)
os.chmod(Path(target), int(mode, 8))
""",
    )


def test_refresh_failure_executes_child_through_verified_lkg(tmp_path: Path) -> None:
    # Given
    fixture = create_fake_network_fixture(tmp_path)
    snapshot = prepare_lkg(fixture)

    # When
    result = fixture.run("sh", "-c", 'printf lkg > "$CHILD_SENTINEL"', mode="binary_fail")

    # Then
    assert_lkg_execution(fixture, result, snapshot)


@pytest.mark.parametrize("mode", ["binary_fail", "manifest_download_fail", "manifest_bad", "skill_fail"])
def test_staging_failures_preserve_lkg_and_reach_downstream(tmp_path: Path, mode: str) -> None:
    # Given
    fixture = create_fake_network_fixture(tmp_path)
    snapshot = prepare_lkg(fixture)
    fixture.configure_release("v1.2.4", "candidate")

    # When
    result = fixture.run("sh", "-c", 'printf lkg > "$CHILD_SENTINEL"', mode=mode)

    # Then
    assert_lkg_execution(fixture, result, snapshot)
    assert not (fixture.state_dir / ".officecli" / "releases" / "v1.2.4").exists()


@pytest.mark.parametrize(
    "redirect",
    [
        "https://evil.example/iOfficeAI/OfficeCLI/releases/tag/v1.2.3",
        "https://github.com/other/OfficeCLI/releases/tag/v1.2.3",
        "https://github.com/iOfficeAI/OfficeCLI/releases/tag/v1.2.3?query=1",
        "https://github.com/iOfficeAI/OfficeCLI/releases/tag/v1.2.3#fragment",
        "https://github.com/iOfficeAI/OfficeCLI/releases/tag/v1.2.3/path",
        "https://github.com/iOfficeAI/OfficeCLI/releases/tag/v1.2.3%2Fescape",
        "https://github.com/iOfficeAI/OfficeCLI/releases/tag/v1.2.3-rc.1",
    ],
)
def test_tag_scoped_downloads_do_not_use_redirect_discovery(tmp_path: Path, redirect: str) -> None:
    # Given
    fixture = create_fake_network_fixture(tmp_path)
    fixture.configure_release("v1.2.3", "candidate")
    fixture.fixture_dir.joinpath("latest-url").write_text(redirect, encoding="utf-8")

    # When
    result = fixture.run("sh", "-c", 'printf reached > "$CHILD_SENTINEL"')

    # Then
    assert result.returncode == 0, result.stderr
    assert fixture.requested_urls() == bootstrap_tests.expected_urls("v1.2.3")


@pytest.mark.parametrize(
    "manifest",
    [
        "a" * 64 + "  officecli-linux-arm64.exe\n",
        "not-a-hash  officecli-linux-arm64\n",
        "a" * 64 + "  officecli-linux-arm64\n" + "b" * 64 + "  officecli-linux-arm64\n",
    ],
)
def test_untrusted_manifests_fail_closed_without_lkg(tmp_path: Path, manifest: str) -> None:
    # Given
    fixture = create_fake_network_fixture(tmp_path)
    fixture.configure_release("v1.2.3", "candidate")
    fixture.fixture_dir.joinpath("SHA256SUMS").write_text(manifest, encoding="utf-8")

    # When
    result = fixture.run("sh", "-c", 'printf reached > "$CHILD_SENTINEL"')

    # Then
    assert result.returncode != 0
    assert not fixture.tencent_sentinel.exists()
    assert not fixture.child_sentinel.exists()


def test_checksum_mismatch_preserves_lkg(tmp_path: Path) -> None:
    # Given
    fixture = create_fake_network_fixture(tmp_path)
    snapshot = prepare_lkg(fixture)
    fixture.configure_release("v1.2.4", "candidate")
    fixture.fixture_dir.joinpath("SHA256SUMS").write_text("0" * 64 + "  officecli-linux-arm64\n", encoding="utf-8")

    # When
    result = fixture.run("sh", "-c", 'printf lkg > "$CHILD_SENTINEL"')

    # Then
    assert_lkg_execution(fixture, result, snapshot)


def test_matching_tampered_manifest_and_binary_block_all_downstream_execution(tmp_path: Path) -> None:
    # Given
    fixture = create_fake_network_fixture(tmp_path)
    fixture.configure_release("v1.2.3", "candidate")
    tampered_binary = b"#!/usr/bin/env sh\nprintf '%s\\n' tampered\n"
    fixture.fixture_dir.joinpath("officecli-linux-arm64").write_bytes(tampered_binary)
    fixture.fixture_dir.joinpath("SHA256SUMS").write_text(
        f"{hashlib.sha256(tampered_binary).hexdigest()}  officecli-linux-arm64\n", encoding="utf-8"
    )

    # When
    result = fixture.run("sh", "-c", 'printf reached > "$CHILD_SENTINEL"')

    # Then
    assert result.returncode != 0
    assert not fixture.tencent_sentinel.exists()
    assert not fixture.child_sentinel.exists()


@pytest.mark.parametrize("mode", ["publish_fail", "activation_fail"])
def test_commit_failures_retain_lkg_and_clean_private_stage(tmp_path: Path, mode: str) -> None:
    # Given
    fixture = create_fake_network_fixture(tmp_path)
    snapshot = prepare_lkg(fixture)
    fixture.configure_release("v1.2.4", "candidate")

    # When
    result = fixture.run("sh", "-c", 'printf lkg > "$CHILD_SENTINEL"', mode=mode)

    # Then
    assert_lkg_execution(fixture, result, snapshot)
    assert not list((fixture.state_dir / ".officecli").glob(".staging.*"))
    assert not list((fixture.state_dir / ".officecli").glob(".current.*"))


def test_metadata_write_failure_retains_lkg(tmp_path: Path) -> None:
    # Given
    fixture = create_fake_network_fixture(tmp_path)
    snapshot = prepare_lkg(fixture)
    fixture.configure_release("v1.2.4", "candidate")
    install_metadata_failure_chmod(fixture)

    # When
    result = fixture.run("sh", "-c", 'printf lkg > "$CHILD_SENTINEL"', mode="metadata_fail")

    # Then
    assert_lkg_execution(fixture, result, snapshot)
    assert not list((fixture.state_dir / ".officecli").glob(".staging.*"))


@pytest.mark.parametrize(
    "corruption",
    [
        "binary_mode",
        "binary_hash",
        "skill_empty",
        "skill_hash",
        "skill_symlink",
        "metadata_executable",
        "metadata_symlink",
        "current_broken",
        "current_external",
        "skill_link_escape",
    ],
)
def test_corrupt_lkg_blocks_all_downstream_execution(tmp_path: Path, corruption: str) -> None:
    # Given
    fixture = create_fake_network_fixture(tmp_path)
    prepare_lkg(fixture)
    root = fixture.state_dir / ".officecli"
    release = root / "releases" / "v1.2.3"
    binary = release / "officecli"
    skill = release / "skill" / "SKILL.md"
    metadata = release / "metadata"
    current = root / "current"
    skill_link = fixture.state_dir / "skills" / "officecli"
    match corruption:
        case "binary_mode":
            binary.chmod(0o600)
        case "binary_hash":
            binary.write_bytes(binary.read_bytes() + b"corrupt")
        case "skill_empty":
            skill.write_bytes(b"")
        case "skill_hash":
            skill.write_bytes(b"changed")
        case "skill_symlink":
            skill.unlink()
            skill.symlink_to(fixture.poison_path)
        case "metadata_executable":
            metadata.chmod(0o700)
        case "metadata_symlink":
            metadata.unlink()
            metadata.symlink_to(fixture.poison_path)
        case "current_broken":
            current.unlink()
            current.symlink_to("releases/v9.9.9")
        case "current_external":
            current.unlink()
            current.symlink_to(tmp_path / "outside")
        case "skill_link_escape":
            skill_link.unlink()
            skill_link.symlink_to("../outside")

    # When
    result = fixture.run("sh", "-c", 'printf reached > "$CHILD_SENTINEL"', mode="binary_fail")

    # Then
    assert result.returncode != 0
    assert not fixture.tencent_sentinel.exists()
    assert not fixture.child_sentinel.exists()


def test_unmanaged_skill_directory_blocks_refresh_without_deletion(tmp_path: Path) -> None:
    # Given
    fixture = create_fake_network_fixture(tmp_path)
    prepare_lkg(fixture)
    fixture.configure_release("v1.2.4", "candidate")
    skill_link = fixture.state_dir / "skills" / "officecli"
    skill_link.unlink()
    skill_link.mkdir()

    # When
    result = fixture.run("sh", "-c", 'printf reached > "$CHILD_SENTINEL"')

    # Then
    assert result.returncode != 0
    assert skill_link.is_dir()
    assert not skill_link.is_symlink()
    assert not fixture.tencent_sentinel.exists()
    assert not fixture.child_sentinel.exists()


def test_curl_disables_config_proxy_and_non_https_protocols(tmp_path: Path) -> None:
    # Given
    fixture = create_fake_network_fixture(tmp_path)
    fixture.configure_release("v1.2.3", "candidate")

    # When
    result = fixture.run("sh", "-c", ":")

    # Then
    assert result.returncode == 0, result.stderr
    for arguments in (line.split("\0") for line in fixture.curl_log.read_text(encoding="utf-8").splitlines()):
        assert arguments[0] in {"--disable", "-q"}
        assert arguments[arguments.index("--config") + 1] == "/dev/null"
        assert arguments[arguments.index("--proto") + 1] == "=https"
        assert arguments[arguments.index("--proto-redir") + 1] == "=https"
        assert arguments[arguments.index("--connect-timeout") + 1] == "15"
        assert arguments[arguments.index("--max-time") + 1] == "120"
        assert arguments[arguments.index("--retry") + 1] == "2"
        assert arguments[arguments.index("--noproxy") + 1] == "*"


def test_linux_fixture_uses_real_mv_until_failure_injection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    monkeypatch.setattr(bootstrap_tests.sys, "platform", "linux")

    # When
    fixture = create_fake_network_fixture(tmp_path)
    fixture.configure_release("v1.2.3", "candidate")
    assert not fixture.bin_dir.joinpath("mv").exists()
    result = fixture.run(mode="activation_fail")

    # Then
    assert result.returncode != 0
    assert fixture.bin_dir.joinpath("mv").exists()


def test_same_tag_drift_retains_existing_immutable_release(tmp_path: Path) -> None:
    # Given
    fixture = create_fake_network_fixture(tmp_path)
    fixture.configure_release("v1.2.3", "original")
    assert fixture.run("sh", "-c", ":").returncode == 0
    release = fixture.state_dir / ".officecli" / "releases" / "v1.2.3"
    original_binary = release.joinpath("officecli").read_bytes()
    original_metadata = release.joinpath("metadata").read_bytes()
    fixture.configure_release("v1.2.3", "drifted")

    # When
    result = fixture.run("sh", "-c", ":")

    # Then
    assert result.returncode == 0, result.stderr
    assert release.joinpath("officecli").read_bytes() == original_binary
    assert release.joinpath("metadata").read_bytes() == original_metadata


def test_activation_replaces_current_with_gnu_atomic_rename(tmp_path: Path, monkeypatch) -> None:
    # Given
    fixture = create_fake_network_fixture(tmp_path)
    fixture.configure_release("v1.2.3", "initial")
    mv_log = tmp_path / "mv.log"
    configure_fake_gnu_mv_log(fixture, monkeypatch, mv_log)

    # When
    result = fixture.run("sh", "-c", ":")

    # Then
    current = fixture.state_dir / ".officecli" / "current"
    assert result.returncode == 0, result.stderr
    assert any(line.startswith("-Tf\0--\0") for line in mv_log.read_text(encoding="utf-8").splitlines())
    assert os.readlink(current) == "releases/v1.2.3"
    assert not list((fixture.state_dir / ".officecli" / "releases").rglob(".current.*"))
