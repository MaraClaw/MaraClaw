from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import anyio
import pytest
from pywhales import ClientNotFoundError
from pywhales.client_config import ParsingError

from app.core.domain.agents import AgentStatus
from app.records.agent import AgentRecord
from app.records.agent_deletion_cleanup import AgentDeletionCleanupRecord
from app.services import agent_deletion_cleanup, agent_stops, company_transfer_cleanup
from app.services.agent_setup_lock import setup_workspace_lock
from app.services.guest_identity import GuestOwnershipError


@pytest.fixture(autouse=True)
def workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(agent_stops.agent_manager, "_agent_dir", lambda agent_id: tmp_path / str(agent_id))


def stopped_agent() -> AgentRecord:
    return AgentRecord(
        id=uuid4(), name="A", creator_id=uuid4(), status=AgentStatus.STOPPED, container_id="old", container_port=3210
    )


async def test_stale_stop_waiting_for_start_keeps_the_new_running_guest(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a stop snapshot queued behind a start holding the real workspace flock.
    stale = stopped_agent()
    waiting = anyio.Event()
    current = stale

    @asynccontextmanager
    async def observed_lock(path: Path, *, timeout_seconds: float) -> AsyncGenerator[None]:
        waiting.set()
        async with setup_workspace_lock(path, timeout_seconds=timeout_seconds):
            yield

    async def read(_agent_id: UUID) -> AgentRecord:
        return replace(current)

    stop = AsyncMock(return_value=True)
    monkeypatch.setattr(agent_stops, "setup_workspace_lock", observed_lock)
    monkeypatch.setattr(agent_stops.agent_dao, "get", read)
    monkeypatch.setattr(agent_stops.agent_manager, "stop_container", stop)
    # When start publishes its new running guest before releasing the lock.
    with anyio.fail_after(3):
        async with (
            anyio.create_task_group() as tasks,
            setup_workspace_lock(agent_stops.agent_manager._agent_dir(stale.id)),
        ):
            tasks.start_soon(agent_stops.stop_containers, [stale])
            await waiting.wait()
            current = replace(stale, status="running", container_id="new", container_port=4321)
    # Then the stale stopped intent never reaches the new guest.
    stop.assert_not_awaited()


@pytest.mark.parametrize("status", ["running", "idle", "error"])
async def test_stop_ignores_a_superseded_stopped_intent(monkeypatch: pytest.MonkeyPatch, status: str) -> None:
    # Given a stale stopped snapshot and a current non-stopped row.
    stale = stopped_agent()
    stop = AsyncMock()
    monkeypatch.setattr(agent_stops.agent_dao, "get", AsyncMock(return_value=replace(stale, status=status)))
    monkeypatch.setattr(agent_stops.agent_manager, "stop_container", stop)
    # When reconciliation processes that snapshot.
    await agent_stops.stop_containers([stale])
    # Then it does not impose an obsolete stop intent.
    stop.assert_not_awaited()


@pytest.mark.parametrize("deleted", [False, True])
async def test_stop_skips_a_current_row_without_a_reference(monkeypatch: pytest.MonkeyPatch, deleted: bool) -> None:
    # Given a stopped snapshot whose current row or reference is gone.
    stale = stopped_agent()
    current = None if deleted else replace(stale, container_id=None)
    stop = AsyncMock()
    monkeypatch.setattr(agent_stops.agent_dao, "get", AsyncMock(return_value=current))
    monkeypatch.setattr(agent_stops.agent_manager, "stop_container", stop)
    # When reconciliation processes the obsolete reference.
    await agent_stops.stop_containers([stale])
    # Then it leaves missing/deleted-row cleanup to the existing backstop.
    stop.assert_not_awaited()


async def test_missing_current_reference_is_persistently_cleared(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a stale snapshot and a different, currently stopped but missing guest.
    stale = stopped_agent()
    current = replace(stale, container_id="missing-current")
    clear = AsyncMock(return_value=True)

    async def missing(agent: AgentRecord) -> bool:
        agent.container_id = None
        return True

    monkeypatch.setattr(agent_stops.agent_dao, "get", AsyncMock(return_value=current))
    monkeypatch.setattr(agent_stops.agent_dao, "clear_missing_stopped_container", clear, raising=False)
    monkeypatch.setattr(agent_stops.agent_manager, "stop_container", missing)
    # When the manager proves that the current stopped guest is missing.
    await agent_stops.stop_containers([stale])
    # Then the CAS targets the current missing reference, not the stale snapshot ID.
    clear.assert_awaited_once_with(current.id, "missing-current")


@pytest.mark.parametrize("outcome", ["stopped", "failed"])
async def test_valid_or_failed_stop_retains_the_reference(monkeypatch: pytest.MonkeyPatch, outcome: str) -> None:
    # Given a valid stopped reference, or an unsuccessful stop without missing proof.
    agent = stopped_agent()
    clear = AsyncMock()

    async def stop(current: AgentRecord) -> bool:
        if outcome == "failed":
            current.container_id = None
        return outcome == "stopped"

    monkeypatch.setattr(agent_stops.agent_dao, "get", AsyncMock(return_value=replace(agent)))
    monkeypatch.setattr(agent_stops.agent_dao, "clear_missing_stopped_container", clear, raising=False)
    monkeypatch.setattr(agent_stops.agent_manager, "stop_container", stop)
    # When reconciliation asks the manager to stop it.
    await agent_stops.stop_containers([agent])
    # Then valid references remain usable by callers/upgrades; failed stops are retried.
    clear.assert_not_awaited()


@pytest.mark.parametrize(
    "fault",
    [None, ParsingError("truncated inspect"), ClientNotFoundError("missing client"), ValueError("invalid stop")],
)
async def test_failed_stop_does_not_abort_later_stops_or_sweep(
    monkeypatch: pytest.MonkeyPatch, fault: Exception | None
) -> None:
    first, second = stopped_agent(), stopped_agent()
    processed: list[UUID | str] = []

    async def read(agent_id: UUID) -> AgentRecord:
        return replace(first if agent_id == first.id else second)

    async def stop(agent: AgentRecord) -> bool:
        if agent.id == first.id:
            if fault is not None:
                raise fault
            raise GuestOwnershipError(agent.id, "old", str(uuid4()))
        processed.append(agent.id)
        return True

    async def sweep(_now: datetime) -> None:
        processed.append("sweep")

    monkeypatch.setattr(agent_stops.agent_dao, "get", read)
    monkeypatch.setattr(agent_stops.agent_manager, "stop_container", stop)
    monkeypatch.setattr(agent_stops.agent_dao, "prepare_container_stops", AsyncMock(return_value=[first, second]))
    monkeypatch.setattr(agent_stops, "reconcile_deletion_cleanup", AsyncMock())
    monkeypatch.setattr(company_transfer_cleanup, "reconcile_transfer_cleanup", AsyncMock())
    monkeypatch.setattr(agent_stops, "sweep_orphan_containers", sweep)

    await agent_stops.reconcile_stopped_containers(datetime.now(UTC))

    assert processed == [second.id, "sweep"]


async def test_busy_stop_is_skipped_and_retried_without_blocking_the_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = stopped_agent(), stopped_agent()
    rows = {agent.id: agent for agent in (first, second)}
    attempted: list[UUID] = []
    held, release = anyio.Event(), anyio.Event()

    async def hold_lock() -> None:
        async with setup_workspace_lock(agent_stops.agent_manager._agent_dir(first.id)):
            held.set()
            await release.wait()

    async def prepare(_now: datetime) -> list[AgentRecord]:
        return [replace(agent) for agent in rows.values() if agent.container_id is not None]

    async def stop(agent: AgentRecord) -> bool:
        attempted.append(agent.id)
        agent.container_id = None
        return True

    async def clear(agent_id: UUID, reference: str) -> bool:
        assert rows[agent_id].container_id == reference
        rows[agent_id] = replace(rows[agent_id], container_id=None)
        return True

    sweep = AsyncMock()
    monkeypatch.setattr(agent_stops, "LIFECYCLE_LOCK_TIMEOUT_SECONDS", 0, raising=False)
    monkeypatch.setattr(agent_stops.agent_dao, "get", AsyncMock(side_effect=lambda agent_id: replace(rows[agent_id])))
    monkeypatch.setattr(agent_stops.agent_dao, "prepare_container_stops", prepare)
    monkeypatch.setattr(agent_stops.agent_dao, "clear_missing_stopped_container", clear)
    monkeypatch.setattr(agent_stops.agent_manager, "stop_container", stop)
    monkeypatch.setattr(agent_stops, "reconcile_deletion_cleanup", AsyncMock())
    monkeypatch.setattr(company_transfer_cleanup, "reconcile_transfer_cleanup", AsyncMock())
    monkeypatch.setattr(agent_stops, "sweep_orphan_containers", sweep)

    now = datetime.now(UTC)
    with anyio.fail_after(2):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(hold_lock)
            await held.wait()
            await agent_stops.reconcile_stopped_containers(now)
            assert attempted == [second.id]
            assert (rows[first.id].status, rows[first.id].container_id) == ("stopped", "old")
            sweep.assert_awaited_once_with(now)
            release.set()

    await agent_stops.reconcile_stopped_containers(now)
    assert attempted == [second.id, first.id]
    assert sweep.await_count == 2


async def test_foreign_deleted_guest_keeps_workspace_and_continues(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given deleted-agent snapshots, the first of which points at a foreign guest.
    first, second = stopped_agent(), stopped_agent()
    archived: list[UUID] = []

    async def remove(agent: AgentRecord) -> bool:
        if agent.id == first.id:
            raise GuestOwnershipError(agent.id, "old", str(uuid4()))
        return True

    async def pending(agent_id: UUID) -> AgentDeletionCleanupRecord:
        return AgentDeletionCleanupRecord(
            agent_id, "Agent", first.creator_id, None, "old", tmp_path / str(agent_id), tmp_path / "archive", "{}"
        )

    def archive(job: AgentDeletionCleanupRecord) -> None:
        archived.append(job.agent_id)

    monkeypatch.setattr(agent_stops.agent_manager, "remove_container", remove)
    monkeypatch.setattr(
        agent_stops.agent_manager, "docker_client", SimpleNamespace(container=SimpleNamespace(list=lambda **_: []))
    )
    monkeypatch.setattr(agent_deletion_cleanup.cleanup, "get_pending", pending)
    monkeypatch.setattr(agent_deletion_cleanup.cleanup, "begin_attempt", AsyncMock())
    monkeypatch.setattr(agent_deletion_cleanup.cleanup, "progress", AsyncMock())
    monkeypatch.setattr(agent_deletion_cleanup, "publish_deletion_archive", archive)
    # When teardown refuses that foreign guest.
    await agent_stops.teardown_deleted_agents([agent_stops.AgentTeardown(agent, None) for agent in (first, second)])
    # Then the foreign writer blocks its archive but not the next agent's cleanup.
    assert archived == [second.id]
