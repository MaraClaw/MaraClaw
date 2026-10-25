"""Snapshot commit and idempotent restore on a real filesystem."""

import uuid

from app.services.openclaw_update_snapshot import commit_snapshot, drop_snapshot, restore_snapshot, snapshot_path


def _agent_dir(tmp_path):
    agent_dir = tmp_path / str(uuid.uuid4())
    (agent_dir / "workspace").mkdir(parents=True)
    (agent_dir / ".openclaw").mkdir()
    (agent_dir / "workspace" / "notes.md").write_text("original", encoding="utf-8")
    (agent_dir / ".openclaw" / "workspace").symlink_to(agent_dir / "workspace")
    return agent_dir


def test_commit_copies_full_state_including_symlinks_and_is_repeatable(tmp_path):
    agent_dir, run_id = _agent_dir(tmp_path), uuid.uuid4()
    snapshot = commit_snapshot(agent_dir, run_id)
    (agent_dir / "workspace" / "notes.md").write_text("changed", encoding="utf-8")
    assert commit_snapshot(agent_dir, run_id) == snapshot
    assert (snapshot / "workspace" / "notes.md").read_text(encoding="utf-8") == "original"
    assert (snapshot / ".openclaw" / "workspace").is_symlink()
    assert not list(snapshot.parent.glob("*.tmp"))


def test_interrupted_copy_leaves_no_committed_snapshot_and_next_commit_succeeds(tmp_path):
    agent_dir, run_id = _agent_dir(tmp_path), uuid.uuid4()
    staging = snapshot_path(agent_dir, run_id).with_name(f"{run_id}.tmp")
    staging.mkdir(parents=True)
    (staging / "partial").write_text("half", encoding="utf-8")
    assert restore_snapshot(agent_dir, run_id) is False
    committed = commit_snapshot(agent_dir, run_id)
    assert not (committed / "partial").exists()
    assert (committed / "workspace" / "notes.md").is_file()


def test_restore_replaces_new_state_and_discards_it_only_on_drop(tmp_path):
    agent_dir, run_id = _agent_dir(tmp_path), uuid.uuid4()
    _ = commit_snapshot(agent_dir, run_id)
    (agent_dir / "workspace" / "notes.md").write_text("new writer", encoding="utf-8")
    assert restore_snapshot(agent_dir, run_id) is True
    assert (agent_dir / "workspace" / "notes.md").read_text(encoding="utf-8") == "original"
    discarded = list(tmp_path.glob("*.rollback-*"))
    assert len(discarded) == 1
    assert restore_snapshot(agent_dir, run_id) is False
    drop_snapshot(agent_dir, run_id)
    assert not list(tmp_path.glob("*.rollback-*"))
    assert not snapshot_path(agent_dir, run_id).exists()


def test_restore_resumes_after_crash_between_the_two_renames(tmp_path):
    # Given step one (agent dir -> discard) finished and the process died before step two.
    agent_dir, run_id = _agent_dir(tmp_path), uuid.uuid4()
    _ = commit_snapshot(agent_dir, run_id)
    (agent_dir / "workspace" / "notes.md").write_text("new writer", encoding="utf-8")
    discard = agent_dir.with_name(f"{agent_dir.name}.rollback-{str(run_id)[:8]}")
    agent_dir.rename(discard)
    assert not agent_dir.exists()
    # When recovery repeats the restore.
    assert restore_snapshot(agent_dir, run_id) is True
    # Then the original state is back and the discarded writer state is still preserved.
    assert (agent_dir / "workspace" / "notes.md").read_text(encoding="utf-8") == "original"
    assert (discard / "workspace" / "notes.md").read_text(encoding="utf-8") == "new writer"
