import asyncio
import fcntl
import multiprocessing
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.records.agent import AgentRecord
from app.services import agent_stops
from app.services.agent_setup_lock import setup_workspace_lock


def late_writer(root: str, agent_id: str, channel) -> None:
    directory = Path(root) / agent_id
    locks = Path(root) / "_setup_locks"
    locks.mkdir(exist_ok=True)
    with (locks / f"{agent_id}.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        directory.mkdir()
        channel.send("writing")
        assert channel.recv() == "finish"
        (directory / "late.md").write_bytes(b"late thread output")
        fcntl.flock(lock, fcntl.LOCK_UN)
    channel.close()


async def test_deletion_waits_cross_process_writer_and_archives_its_late_output(monkeypatch, tmp_path):
    agent = AgentRecord(id=uuid4(), name="Deleted", creator_id=uuid4())
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=late_writer, args=(str(tmp_path), str(agent.id), child))
    attempting = asyncio.Event()
    original_lock = agent_stops.setup_workspace_lock

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def observed_lock(directory):
        attempting.set()
        async with original_lock(directory):
            yield

    monkeypatch.setattr(agent_stops, "setup_workspace_lock", observed_lock)
    monkeypatch.setattr(agent_stops.agent_manager, "_agent_dir", lambda _id: tmp_path / str(_id))
    monkeypatch.setattr("app.services.agent_manager.settings.STORAGE_LOCAL_ROOT", str(tmp_path))
    monkeypatch.setattr(agent_stops.agent_manager, "remove_container", AsyncMock())
    process.start()
    child.close()
    try:
        assert await asyncio.to_thread(parent.poll, 5)
        assert parent.recv() == "writing"
        teardown = asyncio.create_task(agent_stops.teardown_deleted_agents((agent_stops.AgentTeardown(agent, None),)))
        await attempting.wait()
        assert not teardown.done()
        assert not (tmp_path / "_archived").exists()
        parent.send("finish")
        await asyncio.wait_for(teardown, 5)
        await asyncio.to_thread(process.join, 5)
        assert process.exitcode == 0
        assert not (tmp_path / str(agent.id)).exists()
        archived = list((tmp_path / "_archived").glob("*/late.md"))
        assert len(archived) == 1
        assert archived[0].read_bytes() == b"late thread output"
    finally:
        if process.is_alive():
            process.terminate()
            await asyncio.to_thread(process.join, 5)
        parent.close()
        process.close()


async def test_cancelling_waiter_does_not_remove_shared_lock_inode(tmp_path):
    directory = tmp_path / str(uuid4())
    acquired = asyncio.Event()

    async def waiter():
        async with setup_workspace_lock(directory):
            acquired.set()

    async with setup_workspace_lock(directory):
        task = asyncio.create_task(waiter())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not acquired.is_set()
    async with setup_workspace_lock(directory):
        assert (tmp_path / "_setup_locks" / f"{directory.name}.lock").is_file()
