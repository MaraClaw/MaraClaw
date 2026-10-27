import shutil
from pathlib import Path

import pytest

from test_openclaw_officecli_bootstrap import (
    OFFICECLI_AMD64_SHA256,
    OFFICECLI_ARM64_SHA256,
    OFFICECLI_SKILL_SHA256,
    OFFICECLI_TAG,
    RELEASE_ROOT,
    SKILL_ROOT,
    WRAPPER_SOURCE,
    _write_executable,
    create_fake_network_fixture,
)

pytestmark = pytest.mark.parametrize("architecture", ["amd64", "arm64"])


def test_runtime_bootstrap_pins_officecli_to_committed_tag_and_digest(architecture: str) -> None:
    # Given
    source = WRAPPER_SOURCE.read_text(encoding="utf-8")
    digest = {"amd64": OFFICECLI_AMD64_SHA256, "arm64": OFFICECLI_ARM64_SHA256}[architecture]

    # When
    has_committed_digest = f'OFFICECLI_{architecture.upper()}_SHA256="{digest}"' in source

    # Then
    assert has_committed_digest
    assert f'OFFICECLI_TAG="{OFFICECLI_TAG}"' in source
    assert f'OFFICECLI_SKILL_SHA256="{OFFICECLI_SKILL_SHA256}"' in source
    assert "releases/latest" not in source
    assert f"{RELEASE_ROOT}/$OFFICECLI_TAG" in source
    assert f"{SKILL_ROOT}/$OFFICECLI_TAG/SKILL.md" in source


def test_matching_asset_executes_when_installed_then_restarted_offline(tmp_path: Path, architecture: str) -> None:
    # Given
    fixture = create_fake_network_fixture(tmp_path, architecture)
    fixture.configure_release("v1.2.3", architecture)
    installed = fixture.run("officecli", "--version")
    assert installed.returncode == 0, installed.stderr
    assert installed.stdout.strip() == f"officecli {architecture}"
    assert fixture.requested_urls()[0].endswith(f"/{fixture.asset}")

    # When
    result = fixture.run("officecli", "--version", mode="binary_fail")

    # Then
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"officecli {architecture}"
    release = fixture.release("v1.2.3")
    assert f"platform=linux/{architecture}\n" in release.joinpath("metadata").read_text()
    assert not list(release.parent.parent.glob(".staging.*"))


@pytest.mark.parametrize("corruption", ["other-platform", "missing-platform", "duplicate-platform", "unknown-field"])
def test_invalid_cache_metadata_cannot_activate_offline(tmp_path: Path, architecture: str, corruption: str) -> None:
    # Given
    fixture = create_fake_network_fixture(tmp_path, architecture)
    fixture.configure_release("v1.2.3", "cached")
    assert fixture.run("sh", "-c", ":").returncode == 0
    metadata = fixture.release("v1.2.3") / "metadata"
    content = metadata.read_text()
    platform_line = f"platform=linux/{architecture}\n"
    other = "arm64" if architecture == "amd64" else "amd64"
    changes = {
        "other-platform": content.replace(platform_line, f"platform=linux/{other}\n"),
        "missing-platform": content.replace(platform_line, ""),
        "duplicate-platform": content + platform_line,
        "unknown-field": content + "untrusted=value\n",
    }
    metadata.write_text(changes[corruption])

    # When
    result = fixture.run(
        "sh", "-c", 'case "$(command -v officecli)" in */.officecli/*) exit 91;; esac', mode="binary_fail"
    )

    # Then
    assert result.returncode == 0, result.stderr
    assert "OfficeCLI unavailable" in result.stderr
    assert not list(fixture.state_dir.joinpath(".officecli").glob(".staging.*"))


def test_other_target_cache_is_rejected_offline(tmp_path: Path, architecture: str) -> None:
    # Given
    other = "arm64" if architecture == "amd64" else "amd64"
    fixture = create_fake_network_fixture(tmp_path, other)
    fixture.configure_release("v1.2.3", other)
    assert fixture.run("officecli", "--version").returncode == 0
    machine = "x86_64" if architecture == "amd64" else "aarch64"
    _write_executable(
        fixture.bin_dir / "uname", f'#!/bin/sh\ncase "$1" in -s) echo Linux;; -m) echo {machine};; esac\n'
    )

    # When
    offline = fixture.run(
        "sh", "-c", 'case "$(command -v officecli)" in */.officecli/*) exit 91;; esac', mode="binary_fail"
    )

    # Then
    assert offline.returncode == 0, offline.stderr
    assert "OfficeCLI unavailable" in offline.stderr
    assert fixture.release("v1.2.3").is_dir()


@pytest.mark.parametrize(
    "manifest_kind", ["duplicate", "near-name", "other-only", "wrong-digest", "nonhex", "extra-field"]
)
def test_manifest_rejection_is_target_specific_and_cleans_stage(
    tmp_path: Path, architecture: str, manifest_kind: str
) -> None:
    # Given
    fixture = create_fake_network_fixture(tmp_path, architecture)
    fixture.configure_release("v1.2.3", "candidate")
    manifest = fixture.fixture_dir / "SHA256SUMS"
    valid = manifest.read_text()
    cases = {
        "duplicate": valid + valid,
        "near-name": valid + valid.replace(fixture.asset, fixture.asset + ".exe"),
        "other-only": valid.replace(fixture.asset, "officecli-linux-other"),
        "wrong-digest": "0" * 64 + f"  {fixture.asset}\n",
        "nonhex": "g" * 64 + f"  {fixture.asset}\n",
        "extra-field": valid.rstrip() + " extra\n",
    }
    manifest.write_text(cases[manifest_kind])

    # When
    result = fixture.run("openclaw", "gateway")

    # Then
    assert result.returncode == 0, result.stderr
    assert fixture.child_sentinel.read_text() == "gateway"
    assert not fixture.state_dir.joinpath(".officecli", "current").exists()
    assert not list(fixture.state_dir.joinpath(".officecli").glob(".staging.*"))


@pytest.mark.parametrize("mode", ["publish_fail", "activation_fail"])
def test_failed_commit_preserves_executable_cache_and_removes_private_state(
    tmp_path: Path, architecture: str, mode: str
) -> None:
    # Given
    fixture = create_fake_network_fixture(tmp_path, architecture)
    fixture.configure_release("v1.2.3", "cached")
    assert fixture.run("officecli", "--version").returncode == 0
    fixture.configure_release("v1.2.4", "candidate")

    # When
    result = fixture.run("officecli", "--version", mode=mode)

    # Then
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "officecli cached"
    assert not list(fixture.state_dir.joinpath(".officecli").glob(".staging.*"))
    assert not list(fixture.state_dir.joinpath(".officecli").glob(".current.*"))


def test_legacy_unqualified_cache_is_not_activated_offline(tmp_path: Path, architecture: str) -> None:
    # Given
    fixture = create_fake_network_fixture(tmp_path, architecture)
    fixture.configure_release("v1.2.3", "legacy")
    assert fixture.run("sh", "-c", ":").returncode == 0
    root = fixture.state_dir / ".officecli"
    shutil.copytree(fixture.release("v1.2.3"), root / "releases" / "v1.2.3")
    root.joinpath("current").unlink()
    root.joinpath("current").symlink_to("releases/v1.2.3")

    # When
    result = fixture.run(
        "sh", "-c", 'case "$(command -v officecli)" in */.officecli/*) exit 91;; esac', mode="binary_fail"
    )

    # Then
    assert result.returncode == 0, result.stderr
    assert "OfficeCLI unavailable" in result.stderr


def test_unsupported_runtime_fails_before_downloading_or_executing_child(tmp_path: Path, architecture: str) -> None:
    # Given
    fixture = create_fake_network_fixture(tmp_path, architecture)
    _write_executable(fixture.bin_dir / "uname", '#!/bin/sh\ncase "$1" in -s) echo Linux;; -m) echo riscv64;; esac\n')

    # When
    result = fixture.run("openclaw", "gateway")

    # Then
    assert result.returncode != 0
    assert "unsupported OfficeCLI target: Linux/riscv64" in result.stderr
    assert not fixture.child_sentinel.exists()
    assert not fixture.requested_urls()


@pytest.mark.parametrize("cached_path", ["metadata", "officecli", "skill", "skill/SKILL.md", "."])
def test_symlinked_cache_components_cannot_activate_offline(
    tmp_path: Path, architecture: str, cached_path: str
) -> None:
    # Given
    fixture = create_fake_network_fixture(tmp_path, architecture)
    fixture.configure_release("v1.2.3", "cached")
    assert fixture.run("sh", "-c", ":").returncode == 0
    candidate = fixture.release("v1.2.3") / cached_path
    outside = tmp_path / "outside-cache"
    shutil.move(candidate, outside)
    candidate.symlink_to(outside, target_is_directory=outside.is_dir())

    # When
    result = fixture.run(
        "sh", "-c", 'case "$(command -v officecli)" in */.officecli/*) exit 91;; esac', mode="binary_fail"
    )

    # Then
    assert result.returncode == 0, result.stderr
    assert "OfficeCLI unavailable" in result.stderr
    assert not list(fixture.state_dir.joinpath(".officecli").glob(".staging.*"))


def test_changed_same_tag_never_overwrites_validated_release(tmp_path: Path, architecture: str) -> None:
    # Given
    fixture = create_fake_network_fixture(tmp_path, architecture)
    fixture.configure_release("v1.2.3", "immutable")
    assert fixture.run("officecli", "--version").returncode == 0
    before = fixture.release("v1.2.3").joinpath("metadata").read_bytes()
    fixture.configure_release("v1.2.3", "replacement")

    # When
    result = fixture.run("officecli", "--version")

    # Then
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "officecli immutable"
    assert fixture.release("v1.2.3").joinpath("metadata").read_bytes() == before
    assert not list(fixture.state_dir.joinpath(".officecli").glob(".staging.*"))
