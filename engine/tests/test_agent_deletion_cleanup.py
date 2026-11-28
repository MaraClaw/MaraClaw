import errno
import fcntl
import json
import shutil
import sys
from collections.abc import Sequence
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pywhales import DockerClient
from pywhales.components.container import cli_wrapper as container_cli

from app.config import get_settings
from app.records.agent import AgentRecord
from app.records.agent_deletion_cleanup import AgentDeletionCleanupRecord
from app.services import agent_deletion_cleanup as cleanup_module, workspace_mirror
from app.services.agent_deletion_archive import publish_deletion_archive
from app.services.agent_deletion_cleanup import _remove_remaining_writers
from app.services.agent_state_snapshot import agent_prefix
from app.services.guest_identity import AGENT_LABEL, GuestOwnershipError
from host_disk_fakes import FLOOR, full_disk
from state_snapshot_fakes import MemoryStore


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


def test_a_short_host_disk_refuses_the_archive_copy_before_writing_and_keeps_the_workspace(tmp_path, monkeypatch):
    # Given a deleted agent's workspace and a host disk one byte short of its headroom.
    job = make_job(tmp_path)
    disk = full_disk(monkeypatch)
    # When publication tries to copy the workspace into the archive.
    with pytest.raises(OSError) as refused:
        publish_deletion_archive(job)
    # Then it failed as a full disk like any copy failure, wrote no staging or archive tree, and kept the source.
    assert refused.value.errno == errno.ENOSPC
    assert not job.archive_path.parent.exists()
    assert (job.workspace_path / "SOUL.md").read_text() == "Workspace"
    assert disk.asked == [tmp_path]
    # And the replay publishes the archive once the disk has room.
    disk.free = FLOOR
    publish_deletion_archive(job)
    assert (job.archive_path / "SOUL.md").read_text() == "Workspace"
    assert not job.workspace_path.exists()


def test_an_enospc_during_archive_copy_keeps_the_source_and_removes_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = make_job(tmp_path)

    def partial_copy(_source: Path, staging: Path, *, symlinks: bool) -> Path:
        staging.mkdir()
        (staging / "partial").touch()
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(shutil, "copytree", partial_copy)
    with pytest.raises(OSError) as failed:
        publish_deletion_archive(job)
    assert failed.value.errno == errno.ENOSPC
    assert list(job.archive_path.parent.iterdir()) == []
    assert (job.workspace_path / "SOUL.md").read_text() == "Workspace"


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


def test_writer_discovery_emits_all_and_the_ownership_label_filter_with_real_sdk(monkeypatch):
    # Given the installed Docker SDK with only its process execution boundary stubbed.
    agent_id = uuid4()
    client = DockerClient(client_call=[sys.executable])
    commands: list[list[str]] = []

    def run(command: Sequence[str | Path]) -> str:
        commands.append([str(argument) for argument in command])
        return ""

    monkeypatch.setattr(container_cli, "run", run)
    # When cleanup lists remaining writers through the real ContainerCLI.list implementation.
    _remove_remaining_writers(client, agent_id)
    # Then Docker receives --all with exactly the ownership label filter and nothing is removed.
    (command,) = commands
    filters = [value for flag, value in pairwise(command) if flag == "--filter"]
    assert filters == [f"label={AGENT_LABEL}={agent_id}"]
    assert "--all" in command


def test_label_discovered_writers_are_removed_by_inspected_id(monkeypatch):
    # Given an owned duplicate writer discoverable only through its ownership label.
    agent_id = uuid4()
    calls = []

    class Containers:
        def list(self, **kwargs):
            assert kwargs == {"all": True, "filters": [("label", f"{AGENT_LABEL}={agent_id}")]}
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


class CleanupQueue:
    """One job's deletion cleanup row; `progressed` records each (completed, error) the cleanup reports."""

    def __init__(self, job: AgentDeletionCleanupRecord) -> None:
        self.job = job
        self.progressed: list[tuple[bool, str | None]] = []

    async def get_pending(self, _agent_id: UUID) -> AgentDeletionCleanupRecord | None:
        return self.job

    async def begin_attempt(self, _agent_id: UUID) -> None:
        return None

    async def progress(self, _agent_id: UUID, *, completed: bool = False, error: str | None = None) -> None:
        self.progressed.append((completed, error))


type SnapshotDeletion = tuple[AgentDeletionCleanupRecord, CleanupQueue, MemoryStore]


@pytest.fixture
def snapshot_deletion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SnapshotDeletion:
    """A deletion whose guest is gone, an S3 primary holding the agent's snapshots, and both switches off."""
    job = make_job(tmp_path)
    queue = CleanupQueue(job)
    store = MemoryStore({f"{agent_prefix(job.agent_id)}snapshot": b"archive", f"{agent_prefix(uuid4())}other": b"kept"})

    async def removed(_agent: AgentRecord) -> bool:
        return True

    async def no_writers(*_args: object, **_options: object) -> None:
        return None

    monkeypatch.setattr(cleanup_module, "cleanup", queue)
    monkeypatch.setattr(cleanup_module, "run_docker", no_writers)
    monkeypatch.setattr(cleanup_module.agent_manager, "docker_client", SimpleNamespace())
    monkeypatch.setattr(cleanup_module.agent_manager, "remove_container", removed)
    monkeypatch.setattr(workspace_mirror, "get_storage_backend", lambda: store)
    monkeypatch.setattr(get_settings(), "WORKSPACE_MIRROR_ENABLED", False)
    monkeypatch.setattr(get_settings(), "AGENT_STATE_SNAPSHOT_ENABLED", False)
    return job, queue, store


async def test_deletion_deletes_the_agents_snapshots_with_snapshots_off_before_it_completes(
    snapshot_deletion: SnapshotDeletion,
) -> None:
    # Given a pending deletion with snapshots and the mirror both switched off.
    job, queue, store = snapshot_deletion
    # When the cleanup runs.
    await cleanup_module._complete_cleanup(job.agent_id)
    # Then the archive is published, the agent's snapshot prefix is gone, other agents keep theirs, and it completed.
    assert (job.archive_path / "SOUL.md").read_text() == "Workspace"
    assert not any(key.startswith(agent_prefix(job.agent_id)) for key in store.objects)
    assert len(store.objects) == 1
    assert queue.progressed == [(False, None), (True, None)]


async def test_deletion_stays_pending_while_a_snapshot_of_the_agent_runs(snapshot_deletion: SnapshotDeletion) -> None:
    # Given a snapshot or restore of the agent holding its snapshot flock.
    job, queue, store = snapshot_deletion
    before = dict(store.objects)
    lock = job.workspace_path.parent / "_setup_locks" / f"{job.agent_id}.snapshot.lock"
    lock.parent.mkdir(exist_ok=True)
    with lock.open("a+b") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # When the cleanup runs.
        await cleanup_module._complete_cleanup(job.agent_id)
    # Then nothing is deleted and the job records the busy flock instead of completing.
    assert store.objects == before
    [first, (completed, error)] = queue.progressed
    assert (first, completed) == ((False, None), False)
    assert error is not None
    assert error.startswith("SnapshotBusyError")
