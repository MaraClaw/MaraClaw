import asyncio
import json

import anyio
import pytest

from app.api import feishu
from app.dao.channel_config_dao import channel_config_dao
from app.services.channels.stream_source import stream_source
from stream_source_fixtures import connection_config
from test_feishu_acceptance_ack import event_frame, live_frame_adapter

__all__ = ["live_frame_adapter"]


async def test_restart_waits_for_retiring_actual_frame_cleanup(monkeypatch, live_frame_adapter):
    manager, agent_id, clients, connected = live_frame_adapter
    old_client = clients[-1]
    old_runner = manager._tasks[agent_id]
    started, cleaning, release = (asyncio.Event() for _ in range(3))
    committed, acknowledgements, operations = [], [], []

    async def accept(agent, body, *, source):
        assert source.agent_id == agent
        if not started.is_set():
            started.set()
            try:
                await asyncio.Future()
            finally:
                cleaning.set()
                await release.wait()
        committed.append(body)

    async def write(data):
        acknowledgements.append(data)

    monkeypatch.setattr(feishu, "process_feishu_event", accept)
    monkeypatch.setattr(old_client, "_write_message", write)
    payload = json.dumps({"header": {"event_type": "im.message.receive_v1"}, "event": {}}).encode()
    frame = event_frame(payload).SerializeToString()
    callback = asyncio.create_task(old_client._handle_message(frame))
    try:
        await asyncio.wait_for(started.wait(), 1)
        operations.append(asyncio.create_task(manager.stop_client(agent_id)))
        await asyncio.wait_for(cleaning.wait(), 1)
        connected.clear()
        restart = asyncio.create_task(manager.start_client(agent_id, "app", "secret"))
        operations.append(restart)
        with anyio.fail_after(1):
            await anyio.wait_all_tasks_blocked()
        assert not restart.done()
        assert len(clients) == 1
        release.set()
        await asyncio.wait_for(asyncio.gather(*operations), 1)
        await asyncio.wait_for(connected.wait(), 1)
        assert callback.cancelled()
        assert old_runner.done()
        assert len(clients) == 2
        fresh_client = clients[-1]
        assert manager._clients[agent_id] is fresh_client
        assert not manager._tasks[agent_id].done()
        monkeypatch.setattr(fresh_client, "_write_message", write)
        with pytest.raises(asyncio.CancelledError):
            await old_client._handle_message(frame)
        await fresh_client._handle_message(frame)
        assert len(committed) == len(acknowledgements) == 1
    finally:
        release.set()
        callback.cancel()
        await asyncio.gather(callback, *operations, return_exceptions=True)


async def test_reconcile_keeps_an_unchanged_source_and_restarts_or_stops_on_config_changes(
    monkeypatch, live_frame_adapter
):
    manager, agent_id, clients, connected = live_frame_adapter
    original_task = manager._tasks[agent_id]
    rows = [await channel_config_dao.get(manager.live_source(agent_id).config_id)]

    async def list_configured(_channel_type):
        return rows

    monkeypatch.setattr(channel_config_dao, "list_configured", list_configured)

    await manager.reconcile_clients()

    assert len(clients) == 1
    assert manager._tasks[agent_id] is original_task

    connected.clear()
    rotated = connection_config(monkeypatch, agent_id, "feishu", credential="rotated")
    rows[:] = [rotated]
    await manager.reconcile_clients()
    await asyncio.wait_for(connected.wait(), 1)

    assert len(clients) == 2
    assert original_task.done()
    assert manager.live_source(agent_id) == stream_source(rotated)

    rows.clear()
    await manager.reconcile_clients()

    assert manager.tracked_agents() == set()
    assert manager.live_source(agent_id) is None
