"""Snapshot commit and idempotent restore on a real filesystem."""

import errno
import shutil
import uuid
from pathlib import Path

import pytest

from app.services.openclaw_update_snapshot import commit_snapshot, drop_snapshot, restore_snapshot, snapshot_path
from host_disk_fakes import FLOOR, full_disk


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


def test_a_short_host_disk_refuses_the_copy_before_anything_is_written(tmp_path, monkeypatch: pytest.MonkeyPatch):
    # Given an agent directory and a host disk one byte short of its headroom.
    agent_dir, run_id = _agent_dir(tmp_path), uuid.uuid4()
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    disk = full_disk(monkeypatch)
    # When the snapshot is committed.
    with pytest.raises(OSError) as refused:
        _ = commit_snapshot(agent_dir, run_id)
    # Then it failed as a full disk, wrote nothing at all and left the agent directory as it was.
    assert refused.value.errno == errno.ENOSPC
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before
    assert disk.asked == [tmp_path]
    # And the commit goes through once the disk has room.
    disk.free = FLOOR
    assert commit_snapshot(agent_dir, run_id) == snapshot_path(agent_dir, run_id)


def test_a_short_host_disk_still_clears_the_partial_copy_a_crashed_attempt_left(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    # Given a half-written staging copy from a crashed attempt and a host disk that is short.
    agent_dir, run_id = _agent_dir(tmp_path), uuid.uuid4()
    staging = snapshot_path(agent_dir, run_id).with_name(f"{run_id}.tmp")
    staging.mkdir(parents=True)
    (staging / "partial").write_text("half", encoding="utf-8")
    _ = full_disk(monkeypatch)
    # When the snapshot is committed.
    with pytest.raises(OSError):
        _ = commit_snapshot(agent_dir, run_id)
    # Then no partial snapshot remains for the next attempt to find.
    assert not staging.exists()
    assert not snapshot_path(agent_dir, run_id).exists()


def test_an_enospc_during_copy_does_not_leave_staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent_dir, run_id = _agent_dir(tmp_path), uuid.uuid4()

    def partial_copy(_source: Path, staging: Path, *, symlinks: bool) -> Path:
        staging.mkdir()
        (staging / "partial").touch()
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(shutil, "copytree", partial_copy)
    with pytest.raises(OSError) as failed:
        _ = commit_snapshot(agent_dir, run_id)
    assert failed.value.errno == errno.ENOSPC
    assert list(snapshot_path(agent_dir, run_id).parent.iterdir()) == []
    assert (agent_dir / "workspace" / "notes.md").read_text() == "original"


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
