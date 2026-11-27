import fcntl
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import override
from uuid import uuid4

import anyio
import pytest
from botocore.exceptions import ClientError

from app.db.errors import DbError
from app.records.agent import AgentRecord
from app.records.company_transfer import TransferChoice
from app.records.company_transfer_cleanup import TransferCleanupRecord
from app.services import company_transfer_cleanup as service, workspace_mirror
from app.services.agent_state_snapshot import agent_prefix
from app.services.openclaw_update_gate import OpenClawUpdateInProgressError
from state_snapshot_fakes import MemoryStore


async def test_upgrade_exclusion_leaves_durable_cleanup_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    item = TransferCleanupRecord(uuid4(), uuid4(), TransferChoice.FREE)
    attempts: list[tuple[object, str | None]] = []

    async def pending() -> list[TransferCleanupRecord]:
        return [item]

    async def blocked(_item: TransferCleanupRecord) -> str | None:
        raise OpenClawUpdateInProgressError

    async def record(agent_id: object, error: str | None) -> None:
        attempts.append((agent_id, error))

    monkeypatch.setattr(service.cleanup, "pending", pending)
    monkeypatch.setattr(service, "complete_cleanup", blocked)
    monkeypatch.setattr(service.cleanup, "record_attempt", record)
    await service.reconcile_transfer_cleanup()
    assert attempts == [(item.agent_id, "OpenClawUpdateInProgressError")]


async def test_missing_docker_cannot_complete_recorded_container_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = TransferCleanupRecord(uuid4(), uuid4(), TransferChoice.FREE)
    agent = AgentRecord(id=item.agent_id, name="Retained", creator_id=uuid4(), container_id="owned-container")

    async def pending(_id: object) -> bool:
        return True

    async def allowed(_id: object) -> None:
        return None

    async def get(_id: object) -> AgentRecord:
        return agent

    monkeypatch.setattr(service.agent_manager, "_agent_dir", lambda _id: tmp_path / str(item.agent_id))
    monkeypatch.setattr(service.agent_manager, "docker_client", None)
    monkeypatch.setattr(service.cleanup, "is_pending", pending)
    monkeypatch.setattr(service, "assert_no_active_run", allowed)
    monkeypatch.setattr(service.agent_dao, "get", get)
    assert await service.complete_cleanup(item) == "docker_unavailable"


@pytest.mark.parametrize("failure", [DbError("database failed"), RuntimeError("runtime failed")])
async def test_item_and_attempt_recording_failures_do_not_abort_remaining_items(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    # Given two queued items, with a failed first operation and failed attempt persistence.
    items = [TransferCleanupRecord(uuid4(), uuid4(), TransferChoice.FREE) for _ in range(2)]
    visited: list[TransferCleanupRecord] = []
    attempts: list[object] = []

    async def pending() -> list[TransferCleanupRecord]:
        return items

    async def complete(item: TransferCleanupRecord) -> str | None:
        visited.append(item)
        if item == items[0]:
            raise failure
        return None

    async def record(agent_id: object, _error: str | None) -> None:
        attempts.append(agent_id)
        raise DbError("attempt persistence failed")

    monkeypatch.setattr(service.cleanup, "pending", pending)
    monkeypatch.setattr(service, "complete_cleanup", complete)
    monkeypatch.setattr(service.cleanup, "record_attempt", record)
    # When the heartbeat reconciles this batch.
    await service.reconcile_transfer_cleanup()
    # Then the second item is still reached despite both first-item failures.
    assert visited == items
    assert attempts == [items[0].agent_id]


async def test_unavailable_queue_does_not_abort_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a temporarily unavailable cleanup queue.
    async def unavailable() -> list[TransferCleanupRecord]:
        raise DbError("queue unavailable")

    monkeypatch.setattr(service.cleanup, "pending", unavailable)
    # When the heartbeat reaches cleanup.
    await service.reconcile_transfer_cleanup()
    # Then it returns normally so unrelated heartbeat work can proceed.


async def test_contended_workspace_is_recorded_for_retry_without_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a real flock held by an in-flight workspace writer.
    item = TransferCleanupRecord(uuid4(), uuid4(), TransferChoice.FREE)
    locks = tmp_path / "_setup_locks"
    locks.mkdir()
    attempts: list[str | None] = []

    async def pending() -> list[TransferCleanupRecord]:
        return [item]

    async def record(_id: object, error: str | None) -> None:
        attempts.append(error)

    monkeypatch.setattr(service.agent_manager, "_agent_dir", lambda _id: tmp_path / str(item.agent_id))
    monkeypatch.setattr(service.cleanup, "pending", pending)
    monkeypatch.setattr(service.cleanup, "record_attempt", record)
    with (locks / f"{item.agent_id}.lock").open("a+b") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # When cleanup encounters the busy workspace.
        with anyio.fail_after(1):
            await service.reconcile_transfer_cleanup()
    # Then the item remains retryable instead of blocking the batch.
    assert attempts == ["workspace_busy"]


class RecordingStore(MemoryStore):
    """An S3 primary that records a finished prefix deletion among the cleanup's other steps."""

    def __init__(self, events: list[object], objects: Mapping[str, bytes]) -> None:
        super().__init__(objects)
        self.events: list[object] = events

    @override
    async def delete_tree(self, key: str) -> None:
        await super().delete_tree(key)
        self.events.append("delete snapshots")


type SnapshotTransfer = tuple[TransferCleanupRecord, list[object], RecordingStore, Path]


@pytest.fixture
def snapshot_transfer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SnapshotTransfer:
    """A free transfer's queued cleanup that reaches the credential scrub; each later step is recorded in order."""
    item = TransferCleanupRecord(uuid4(), uuid4(), TransferChoice.FREE)
    agent = AgentRecord(id=item.agent_id, name="Moved", creator_id=uuid4())
    agent_dir = tmp_path / "agents" / str(item.agent_id)
    events: list[object] = []
    store = RecordingStore(events, {f"{agent_prefix(agent.id)}snapshot": b"archive", f"{uuid4()}/AGENTS.md": b"kept"})

    async def pending() -> list[TransferCleanupRecord]:
        return [item]

    async def yes(_id: object) -> bool:
        return True

    async def allowed(_id: object) -> None:
        return None

    async def get(_id: object) -> AgentRecord:
        return agent

    async def removed(_agent: AgentRecord) -> bool:
        return True

    async def transfer_row(_id: object) -> SimpleNamespace:
        return SimpleNamespace(source_tenant_id=uuid4())

    @asynccontextmanager
    async def no_tools() -> AsyncGenerator[SimpleNamespace]:
        async def fetchall(_query: str, _params: object) -> list[object]:
            return []

        yield SimpleNamespace(fetchall=fetchall)

    def scrub(_agent_dir: Path, *, free: bool, source_tool_names: frozenset[str]) -> None:
        events.append("scrub")

    async def detach(*, db_obj: AgentRecord, obj_in: object) -> AgentRecord:
        events.append("detach")
        return db_obj

    async def record(_id: object, error: str | None) -> None:
        events.append(("attempt", error))

    monkeypatch.setattr(service.agent_manager, "_agent_dir", lambda _id: agent_dir)
    monkeypatch.setattr(service.agent_manager, "remove_container", removed)
    monkeypatch.setattr(service.cleanup, "pending", pending)
    monkeypatch.setattr(service.cleanup, "is_pending", yes)
    monkeypatch.setattr(service.cleanup, "record_attempt", record)
    monkeypatch.setattr(service, "assert_no_active_run", allowed)
    monkeypatch.setattr(service.agent_dao, "get", get)
    monkeypatch.setattr(service.agent_dao, "update", detach)
    monkeypatch.setattr(service.company_transfer_dao, "get", transfer_row)
    monkeypatch.setattr(service, "connection_ctx", no_tools)
    monkeypatch.setattr(service, "stop_free_transfer_connectors", allowed)
    monkeypatch.setattr(service, "scrub_credentials", scrub)
    monkeypatch.setattr(workspace_mirror, "get_storage_backend", lambda: store)
    return item, events, store, agent_dir


async def test_transfer_cleanup_deletes_the_agents_snapshots_after_the_scrub(
    snapshot_transfer: SnapshotTransfer,
) -> None:
    # Given a queued transfer cleanup and the agent's snapshots of its old tenant.
    item, events, store, _agent_dir = snapshot_transfer
    # When the cleanup runs.
    _ = await service.reconcile_transfer_cleanup()
    # Then the snapshots go after the credentials are scrubbed and before the agent is detached and the item done.
    assert events == ["scrub", "delete snapshots", "detach", ("attempt", None)]
    assert not any(key.startswith(agent_prefix(item.agent_id)) for key in store.objects)
    assert len(store.objects) == 1


@pytest.mark.parametrize(("failure", "error"), [("busy", "snapshot_busy"), ("storage", "snapshot_delete_failed")])
async def test_a_busy_or_failed_snapshot_deletion_leaves_the_transfer_cleanup_pending(
    snapshot_transfer: SnapshotTransfer, failure: str, error: str
) -> None:
    # Given a snapshot of the agent in progress, or storage failing to delete.
    _item, events, store, agent_dir = snapshot_transfer
    before = dict(store.objects)
    if failure == "storage":
        store.failures["delete"] = ClientError({"Error": {"Code": "InternalError", "Message": "S3"}}, "DeleteObjects")
    lock = agent_dir.parent / "_setup_locks" / f"{agent_dir.name}.snapshot.lock"
    lock.parent.mkdir(parents=True)
    with lock.open("a+b") as held:
        if failure == "busy":
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # When the cleanup runs.
        _ = await service.reconcile_transfer_cleanup()
    # Then the item stays pending under its own error, with the agent still attached and the snapshots kept.
    assert events == ["scrub", ("attempt", error)]
    assert store.objects == before
