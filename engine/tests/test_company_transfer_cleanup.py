import fcntl
from pathlib import Path
from uuid import uuid4

import anyio
import pytest

from app.db.errors import DbError
from app.records.agent import AgentRecord
from app.records.company_transfer import TransferChoice
from app.records.company_transfer_cleanup import TransferCleanupRecord
from app.services import company_transfer_cleanup as service
from app.services.openclaw_update_gate import OpenClawUpdateInProgressError


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
