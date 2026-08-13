from pathlib import Path

import pytest

from openclaw_tencentdb_patch_verifier_fixtures import (
    assert_node_syntax,
    assert_redacted_failure,
    backup_path,
    decoy_source,
    run_verifier,
    selected_hook_source,
    write_noop_patch_asset,
    write_patch_asset,
)


def _root_with_dist(tmp_path: Path) -> Path:
    root = tmp_path / "canary-openclaw"
    (root / "dist").mkdir(parents=True)
    return root


def _write_target(root: Path, name: str, source: str | None = None) -> Path:
    target = root / "dist" / name
    target.write_text(source or selected_hook_source(), encoding="utf-8")
    return target


@pytest.mark.parametrize(
    ("mode", "target_count", "expected"),
    [
        ("all", 0, "stage=candidate code=no-target"),
        ("partial", 2, "stage=verify code=target-untransformed"),
        ("missing-backup", 1, "stage=backup code=missing-or-invalid"),
    ],
)
def test_verifier_emits_one_redacted_allowlisted_failure(
    tmp_path: Path, mode: str, target_count: int, expected: str
) -> None:
    # Given
    root = _root_with_dist(tmp_path)
    for name in ("target-a.js", "target-b.js")[:target_count]:
        _write_target(root, name)
    asset = tmp_path / "patch.sh"
    write_patch_asset(asset)

    # When
    result = run_verifier(root, asset, mode=mode)

    # Then
    assert_redacted_failure(result, expected, str(root), "target-a.js", "target-b.js")


@pytest.mark.parametrize("backup_kind", ["symlink", "directory"])
def test_verifier_rejects_unsafe_preexisting_backup(tmp_path: Path, backup_kind: str) -> None:
    # Given
    root = _root_with_dist(tmp_path)
    target = _write_target(root, "arbitrary-target.js")
    backup = backup_path(target)
    match backup_kind:
        case "symlink":
            backup.symlink_to(tmp_path / "outside")
        case "directory":
            backup.mkdir()
        case unreachable:
            pytest.fail(f"unsupported backup fixture: {unreachable}")
    asset = tmp_path / "no-op-patch.sh"
    write_noop_patch_asset(asset)

    # When
    result = run_verifier(root, asset)

    # Then
    assert_redacted_failure(result, "stage=backup code=missing-or-invalid", str(root), target.name, backup.name)


def test_verifier_rejects_backup_that_is_not_the_target_preimage(tmp_path: Path) -> None:
    # Given
    root = _root_with_dist(tmp_path)
    target = _write_target(root, "arbitrary-target.js")
    asset = tmp_path / "patch.sh"
    write_patch_asset(asset)

    # When
    result = run_verifier(root, asset, mode="wrong-backup")

    # Then
    assert_redacted_failure(result, "stage=backup code=digest-mismatch", str(root), target.name)


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("first-non-target-drift", "stage=verify code=non-target-drift"),
        ("second-target-drift", "stage=idempotence code=target-drift"),
        ("second-backup-drift", "stage=idempotence code=backup-drift"),
        ("second-non-target-drift", "stage=idempotence code=target-drift"),
    ],
)
def test_verifier_rejects_first_or_internal_second_run_drift(tmp_path: Path, mode: str, expected: str) -> None:
    # Given
    root = _root_with_dist(tmp_path)
    target = _write_target(root, "target-a.js")
    decoy = _write_target(root, "arbitrary-decoy.js", decoy_source())
    asset = tmp_path / "patch.sh"
    write_patch_asset(asset)

    # When
    result = run_verifier(root, asset, mode=mode)

    # Then
    assert_redacted_failure(result, expected, str(root), target.name, decoy.name)


def test_verifier_rejects_unrelated_messages_marker_in_unpatched_hook(tmp_path: Path) -> None:
    # Given
    root = _root_with_dist(tmp_path)
    target = _write_target(
        root,
        "unpatched-hook.js",
        """const after_tool_call = true;
const unrelatedMarker = {
  durationMs,
  messages : ctx.params.session?.messages
};
const hookEvent = {
  durationMs
};
hookRunnerAfter();
""",
    )
    original_target = target.read_bytes()
    asset = tmp_path / "no-op-patch.sh"
    write_noop_patch_asset(asset)

    # When
    result = run_verifier(root, asset)

    # Then
    assert_redacted_failure(result, "stage=verify code=target-untransformed", str(root), target.name)
    assert target.read_bytes() == original_target
    assert not backup_path(target).exists()


def test_verifier_rejects_prepatched_hook_without_an_immediate_runner(tmp_path: Path) -> None:
    # Given
    root = _root_with_dist(tmp_path)
    target = _write_target(
        root,
        "arbitrary-runner-gap.js",
        """const after_tool_call = true;
const hookEvent = {
  messages: ctx.params.session?.messages,
  durationMs
};
const unrelated = true;
hookRunnerAfter();
""",
    )
    original_target = target.read_bytes()
    asset = tmp_path / "no-op-patch.sh"
    write_noop_patch_asset(asset)

    # When
    result = run_verifier(root, asset)

    # Then
    assert_redacted_failure(result, "stage=internal code=unclassified", str(root), target.name)
    assert target.read_bytes() == original_target
    assert not backup_path(target).exists()


def test_verifier_rejects_later_shadowed_hook_event(tmp_path: Path) -> None:
    # Given
    root = _root_with_dist(tmp_path)
    target = _write_target(
        root,
        "arbitrary-shadowed-hook.js",
        """const after_tool_call = true;
const hookEvent = {
  durationMs
};
hookRunnerAfter();
const nested = () => {
  const hookEvent = {
    messages : ctx.params.session?.messages,
    durationMs
  };
};
""",
    )
    original_target = target.read_bytes()
    asset = tmp_path / "no-op-patch.sh"
    write_noop_patch_asset(asset)
    assert_node_syntax(target)

    # When
    result = run_verifier(root, asset)

    # Then
    assert_redacted_failure(result, "stage=verify code=target-untransformed", str(root), target.name)
    assert target.read_bytes() == original_target
    assert not backup_path(target).exists()
