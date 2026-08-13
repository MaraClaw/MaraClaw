from pathlib import Path

from openclaw_tencentdb_patch_verifier_fixtures import (
    EXACT_MESSAGES_PROPERTY,
    assert_node_syntax,
    backup_path,
    create_mixed_patch_scenario,
    run_verifier,
    write_patch_asset,
)


def test_mixed_selected_states_preserve_prepatched_and_transform_unpatched(tmp_path: Path) -> None:
    # Given
    scenario = create_mixed_patch_scenario(tmp_path)
    asset = tmp_path / "whitespace-aware-patch.sh"
    write_patch_asset(asset)
    original_prepatched = (
        scenario.prepatched_without_backup.read_bytes(),
        scenario.prepatched_with_backup.read_bytes(),
    )
    original_unpatched = tuple(target.read_bytes() for target in scenario.unpatched_targets)
    original_decoy = scenario.decoy.read_bytes()

    # When
    result = run_verifier(scenario.root, asset)

    # Then
    assert result.returncode == 0, result.stderr
    assert scenario.prepatched_without_backup.read_bytes() == original_prepatched[0]
    assert not backup_path(scenario.prepatched_without_backup).exists()
    assert scenario.prepatched_with_backup.read_bytes() == original_prepatched[1]
    assert scenario.existing_backup.read_bytes() == scenario.existing_backup_bytes
    for target, original in zip(scenario.unpatched_targets, original_unpatched, strict=True):
        backup = backup_path(target)
        assert target.read_bytes() != original
        assert EXACT_MESSAGES_PROPERTY in target.read_text(encoding="utf-8")
        assert backup.is_file()
        assert not backup.is_symlink()
        assert backup.read_bytes() == original
    assert scenario.decoy.read_bytes() == original_decoy
    assert_node_syntax(
        scenario.prepatched_without_backup,
        scenario.prepatched_with_backup,
        *scenario.unpatched_targets,
        scenario.decoy,
    )


def test_external_second_verifier_invocation_is_byte_and_backup_stable(tmp_path: Path) -> None:
    # Given
    scenario = create_mixed_patch_scenario(tmp_path)
    asset = tmp_path / "patch.sh"
    write_patch_asset(asset)
    first_result = run_verifier(scenario.root, asset)
    assert first_result.returncode == 0, first_result.stderr
    targets = (
        scenario.prepatched_without_backup,
        scenario.prepatched_with_backup,
        *scenario.unpatched_targets,
        scenario.decoy,
    )
    target_bytes = tuple(target.read_bytes() for target in targets)
    backup_bytes = tuple(
        backup_path(target).read_bytes() if backup_path(target).exists() else None for target in targets
    )

    # When
    result = run_verifier(scenario.root, asset)

    # Then
    assert result.returncode == 0, result.stderr
    assert tuple(target.read_bytes() for target in targets) == target_bytes
    assert (
        tuple(backup_path(target).read_bytes() if backup_path(target).exists() else None for target in targets)
        == backup_bytes
    )
    assert_node_syntax(*targets)
