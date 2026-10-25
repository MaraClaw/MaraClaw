import errno
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from python_on_whales import DockerClient

from app.records.agent_deletion_cleanup import AgentDeletionCleanupRecord
from app.services.agent_deletion_archive import publish_deletion_archive
from app.services.agent_deletion_cleanup import _remove_remaining_writers
from app.services.guest_identity import AGENT_LABEL, GuestOwnershipError


def make_job(root: Path) -> AgentDeletionCleanupRecord:
    agent_id = uuid4()
    source, target = root / "workspaces" / str(agent_id), root / "storage" / str(agent_id)
    source.mkdir(parents=True)
    (source / "SOUL.md").write_text("Workspace")
    return AgentDeletionCleanupRecord(
        agent_id, "Agent", uuid4(), None, None, source, target, json.dumps({"agent_id": str(agent_id), "tasks": []})
    )


def test_archive_copy_publishes_with_only_a_same_directory_rename(tmp_path, monkeypatch):
    # Given archive storage on a different filesystem from the source workspace.
    job = make_job(tmp_path)
    rename = Path.rename

    def cross_device_rename(source: Path, target: Path) -> Path:
        if source.parent != target.parent:
            raise OSError(errno.EXDEV, "cross-device rename")
        return rename(source, target)

    monkeypatch.setattr(Path, "rename", cross_device_rename)
    # When publication copies data to the archive filesystem and replay runs again.
    publish_deletion_archive(job)
    publish_deletion_archive(job)
    # Then the published tree is complete and unique despite cross-device rename restrictions.
    assert not job.workspace_path.exists()
    assert (job.archive_path / "SOUL.md").read_text() == "Workspace"
    assert (job.archive_path / "task_history.json").read_text() == job.task_history
    assert list(job.archive_path.parent.iterdir()) == [job.archive_path]


@pytest.mark.parametrize("conflict", ["identity", "history"])
def test_archive_conflict_preserves_source_for_operator_recovery(tmp_path, conflict):
    # Given a destination that does not match this cleanup's identity or frozen history.
    job = make_job(tmp_path)
    job.archive_path.mkdir(parents=True)
    (job.archive_path / ".deletion-cleanup").write_text("foreign" if conflict == "identity" else str(job.agent_id))
    (job.archive_path / "task_history.json").write_text("foreign" if conflict == "history" else job.task_history)
    # When cleanup encounters that conflicting publication.
    with pytest.raises(OSError):
        publish_deletion_archive(job)
    # Then it never destroys the remaining source to hide an archive conflict.
    assert (job.workspace_path / "SOUL.md").read_text() == "Workspace"


def test_label_discovered_writers_are_removed_by_inspected_id(monkeypatch):
    # Given an owned duplicate writer discoverable only through its ownership label.
    agent_id = uuid4()
    calls = []

    class Containers:
        def list(self, **kwargs):
            assert kwargs == {"all": True, "filters": {"label": f"{AGENT_LABEL}={agent_id}"}}
            return [SimpleNamespace(id="full-inspected-id")]

        def inspect(self, reference):
            assert reference == "full-inspected-id"
            return SimpleNamespace(id=reference, config=SimpleNamespace(labels={AGENT_LABEL: str(agent_id)}))

        def stop(self, reference, *, time):
            calls.append(("stop", reference))

        def remove(self, references):
            calls.append(("remove", references))

    # When cleanup removes the remaining writer.
    client = DockerClient()
    monkeypatch.setattr(client, "container", Containers())
    _remove_remaining_writers(client, agent_id)
    # Then destructive commands use only the inspected immutable ID.
    assert calls == [("stop", "full-inspected-id"), ("remove", ["full-inspected-id"])]


def test_label_discovery_does_not_remove_a_foreign_inspected_guest(monkeypatch):
    # Given a listing whose inspected ownership no longer matches the deleted agent.
    agent_id = uuid4()
    calls = []

    class Containers:
        def list(self, **kwargs):
            return [SimpleNamespace(id="foreign-id")]

        def inspect(self, reference):
            return SimpleNamespace(id=reference, config=SimpleNamespace(labels={AGENT_LABEL: str(uuid4())}))

        def stop(self, reference, **kwargs):
            calls.append(reference)

        def remove(self, references):
            calls.extend(references)

    # When ownership is checked immediately before destructive work.
    client = DockerClient()
    monkeypatch.setattr(client, "container", Containers())
    with pytest.raises(GuestOwnershipError):
        _remove_remaining_writers(client, agent_id)
    # Then neither stop nor remove reaches a foreign guest.
    assert calls == []
