from pathlib import Path

import pytest

from app.services.openclaw_gateway_config import (
    _ADAPTER,
    ensure_gateway_token,
    forget_native_retirement,
    prepare_native_gateway_files,
)

LEGACY_BOOTSTRAP = b"""# Bootstrap

There is no extra founding ritual file for this agent.

Use `SOUL.md`, `IDENTITY.md`, and the `maraclaw-sync` skill.
On every wake, poll the MaraClaw inbox, answer, and report.
"""


@pytest.mark.parametrize("modified", [False, True])
def test_retirement_replaces_only_exact_generated_bootstrap(tmp_path: Path, modified: bool) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bootstrap = workspace / "BOOTSTRAP.md"
    original = LEGACY_BOOTSTRAP + (b"User customization\n" if modified else b"")
    bootstrap.write_bytes(original)

    prepare_native_gateway_files(tmp_path)

    assert (bootstrap.read_bytes() == original) is modified


def test_retirement_preserves_modified_assets_and_gateway_token(tmp_path: Path) -> None:
    skills = tmp_path / "workspace" / "skills"
    skill = skills / "maraclaw-sync"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("user customization", encoding="utf-8")
    (tmp_path / ".maraclaw-poll.mjs").write_text("custom script", encoding="utf-8")
    (tmp_path / ".openclaw-gateway-token").write_text("existing-token", encoding="utf-8")

    prepare_native_gateway_files(tmp_path)

    assert not skill.exists()
    (retired_skill,) = tmp_path.glob(".maraclaw-retired-sync-*")
    assert (retired_skill / "SKILL.md").read_text(encoding="utf-8") == "user customization"
    (retired_script,) = tmp_path.glob(".maraclaw-retired-*-maraclaw-poll.mjs")
    assert retired_script.read_text(encoding="utf-8") == "custom script"
    assert ensure_gateway_token(tmp_path) == "existing-token"
    assert (tmp_path / ".maraclaw-rpc.mjs").is_file()


def test_retirement_does_not_follow_legacy_symlinks(tmp_path: Path) -> None:
    target = tmp_path / "user-data"
    target.write_text("keep", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "BOOTSTRAP.md").symlink_to(target)
    (tmp_path / ".maraclaw-wake.mjs").symlink_to(target)

    prepare_native_gateway_files(tmp_path)

    (retired,) = tmp_path.glob(".maraclaw-retired-*-maraclaw-wake.mjs")
    assert retired.is_symlink()
    assert (workspace / "BOOTSTRAP.md").is_symlink()
    assert target.read_text(encoding="utf-8") == "keep"


def test_staging_refuses_to_replace_rpc_symlink(tmp_path: Path) -> None:
    target = tmp_path / "user-data"
    target.write_text("keep", encoding="utf-8")
    (tmp_path / ".maraclaw-rpc.mjs").symlink_to(target)

    with pytest.raises(OSError):
        prepare_native_gateway_files(tmp_path)

    assert (tmp_path / ".maraclaw-rpc.mjs").is_symlink()
    assert target.read_text(encoding="utf-8") == "keep"


def test_retirement_refuses_to_traverse_workspace_symlink(tmp_path: Path) -> None:
    target = tmp_path / "user-workspace"
    target.mkdir()
    bootstrap = target / "BOOTSTRAP.md"
    bootstrap.write_bytes(LEGACY_BOOTSTRAP)
    (tmp_path / "workspace").symlink_to(target, target_is_directory=True)

    with pytest.raises(OSError):
        prepare_native_gateway_files(tmp_path)

    assert bootstrap.read_bytes() == LEGACY_BOOTSTRAP


def test_token_creation_refuses_symlink(tmp_path: Path) -> None:
    target = tmp_path / "user-data"
    target.write_text("keep", encoding="utf-8")
    (tmp_path / ".openclaw-gateway-token").symlink_to(target)

    with pytest.raises(OSError):
        ensure_gateway_token(tmp_path)

    assert target.read_text(encoding="utf-8") == "keep"


def test_unchanged_adapter_is_not_rewritten_and_a_changed_one_is(tmp_path: Path) -> None:
    adapter = tmp_path / ".maraclaw-rpc.mjs"

    prepare_native_gateway_files(tmp_path)
    staged = adapter.stat()
    prepare_native_gateway_files(tmp_path)

    assert adapter.stat().st_ino == staged.st_ino
    adapter.write_text("tampered", encoding="utf-8")
    prepare_native_gateway_files(tmp_path)
    assert adapter.read_bytes() == _ADAPTER.read_bytes()


def test_legacy_retirement_runs_once_per_directory_until_forgotten(tmp_path: Path) -> None:
    prepare_native_gateway_files(tmp_path)
    legacy = tmp_path / ".maraclaw-poll.mjs"
    legacy.write_text("late legacy script", encoding="utf-8")

    prepare_native_gateway_files(tmp_path)
    assert legacy.exists()

    forget_native_retirement(tmp_path)
    prepare_native_gateway_files(tmp_path)
    assert not legacy.exists()
    (retired,) = tmp_path.glob(".maraclaw-retired-*-maraclaw-poll.mjs")
    assert retired.read_text(encoding="utf-8") == "late legacy script"
