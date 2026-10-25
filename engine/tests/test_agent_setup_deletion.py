import asyncio
import multiprocessing
from multiprocessing.connection import wait
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from agent_setup_process_fixtures import late_writer
from app.records.agent import AgentRecord
from app.records.agent_deletion_cleanup import AgentDeletionCleanupRecord
from app.services import agent_deletion_cleanup, agent_stops
from app.services.agent_setup_lock import setup_workspace_lock


async def test_deletion_defers_busy_cross_process_writer_and_archives_its_late_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given durable cleanup and a real process still writing under the workspace flock.
    agent = AgentRecord(id=uuid4(), name="Deleted", creator_id=uuid4())
    job = AgentDeletionCleanupRecord(
        agent_id=agent.id,
        name=agent.name,
        creator_id=agent.creator_id,
        tenant_id=agent.tenant_id,
        container_id=agent.container_id,
        workspace_path=tmp_path / str(agent.id),
        archive_path=tmp_path / "_archived" / f"{agent.id}_deleted",
        task_history='{"tasks":[]}',
    )
    pending = {agent.id: job}

    async def progress(agent_id: UUID, *, completed: bool = False, error: str | None = None) -> None:
        assert error is None
        if completed:
            del pending[agent_id]

    begin_attempt = AsyncMock()
    report_progress = AsyncMock(side_effect=progress)
    remove_container = AsyncMock(return_value=True)
    monkeypatch.setattr(agent_deletion_cleanup.cleanup, "get_pending", AsyncMock(side_effect=pending.get))
    monkeypatch.setattr(agent_deletion_cleanup.cleanup, "pending", AsyncMock(side_effect=lambda: list(pending)))
    monkeypatch.setattr(agent_deletion_cleanup.cleanup, "begin_attempt", begin_attempt)
    monkeypatch.setattr(agent_deletion_cleanup.cleanup, "progress", report_progress)
    monkeypatch.setattr(
        agent_deletion_cleanup,
        "agent_manager",
        SimpleNamespace(
            docker_client=SimpleNamespace(container=SimpleNamespace(list=lambda **_kwargs: [])),
            remove_container=remove_container,
        ),
    )
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=late_writer, args=(str(tmp_path), str(agent.id), child))
    process.start()
    child.close()
    try:
        ready = await asyncio.to_thread(wait, (parent, process.sentinel), 10)
        assert parent in ready, f"writer did not signal readiness: exitcode={process.exitcode}"
        assert parent.recv() == "writing"
        # When the accelerator encounters the busy lock, it returns without consuming the job.
        await asyncio.wait_for(
            agent_stops.teardown_deleted_agents((agent_stops.AgentTeardown(agent, job.task_history),)), 5
        )
        assert pending == {agent.id: job}
        begin_attempt.assert_not_awaited()
        report_progress.assert_not_awaited()
        remove_container.assert_not_awaited()
        assert job.workspace_path.is_dir()
        assert not (tmp_path / "_archived").exists()
        parent.send("finish")
        await asyncio.to_thread(process.join, 5)
        assert process.exitcode == 0
        # Then a later worker replays the same job, including the writer's final output, once.
        for _ in range(2):
            await asyncio.wait_for(agent_deletion_cleanup.reconcile_deletion_cleanup(), 5)
        assert pending == {}
        begin_attempt.assert_awaited_once_with(agent.id)
        remove_container.assert_awaited_once()
        assert report_progress.await_args_list == [((agent.id,), {}), ((agent.id,), {"completed": True})]
        assert not job.workspace_path.exists()
        archived = list((tmp_path / "_archived").glob("*/late.md"))
        assert len(archived) == 1
        assert archived[0].read_bytes() == b"late thread output"
        assert (job.archive_path / "task_history.json").read_text() == job.task_history
    finally:
        if process.is_alive():
            process.terminate()
            await asyncio.to_thread(process.join, 5)
        parent.close()
        process.close()


async def test_cancelling_waiter_does_not_remove_shared_lock_inode(tmp_path: Path) -> None:
    directory = tmp_path / str(uuid4())
    acquired = asyncio.Event()
    attempting = asyncio.Event()

    async def waiter() -> None:
        attempting.set()
        async with setup_workspace_lock(directory):
            acquired.set()

    async with setup_workspace_lock(directory):
        task = asyncio.create_task(waiter())
        await asyncio.wait_for(attempting.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
    assert not acquired.is_set()
    async with setup_workspace_lock(directory, timeout_seconds=5):
        assert (tmp_path / "_setup_locks" / f"{directory.name}.lock").is_file()
