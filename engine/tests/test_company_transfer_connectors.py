import asyncio
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import anyio
import pytest

from app.config import get_settings
from app.services import company_transfer_connectors as connectors
from app.services.dingtalk_stream import dingtalk_stream_manager
from app.services.discord_gateway import discord_gateway_manager
from app.services.feishu_ws import feishu_ws_manager
from app.services.wechat_channel import wechat_poll_manager
from app.services.wecom_stream import wecom_stream_manager


@dataclass
class ActiveConnectors:
    agent_id: UUID
    tasks: list[asyncio.Task[None]]
    thread: threading.Thread


@pytest.fixture
async def active_connectors(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[ActiveConnectors]:
    agent_id = uuid4()
    tasks: list[asyncio.Task[None]] = []
    for manager in (feishu_ws_manager, wecom_stream_manager, wechat_poll_manager, discord_gateway_manager):
        started = anyio.Event()

        async def run(signal: anyio.Event = started) -> None:
            signal.set()
            await anyio.sleep_forever()

        task = asyncio.create_task(run())
        tasks.append(task)
        monkeypatch.setattr(manager, "_tasks", {agent_id: task})
        await started.wait()
    for manager in (feishu_ws_manager, wecom_stream_manager, discord_gateway_manager):
        monkeypatch.setattr(manager, "_clients", {})
    monkeypatch.setattr(feishu_ws_manager, "_ping_tasks", {})
    monkeypatch.setattr(wecom_stream_manager, "_connected", {agent_id: True})
    monkeypatch.setattr(wechat_poll_manager, "_connected", {agent_id: True})

    async def set_connected(_agent_id: UUID, _connected: bool) -> None:
        return None

    monkeypatch.setattr(wechat_poll_manager, "_set_connected", set_connected)
    stop = threading.Event()
    thread = threading.Thread(target=stop.wait)
    thread.start()
    monkeypatch.setattr(dingtalk_stream_manager, "_threads", {agent_id: thread})
    monkeypatch.setattr(dingtalk_stream_manager, "_stop_events", {agent_id: stop})
    try:
        yield ActiveConnectors(agent_id, tasks, thread)
    finally:
        for task in tasks:
            _ = task.cancel()
        _ = await asyncio.gather(*tasks, return_exceptions=True)
        stop.set()
        await anyio.to_thread.run_sync(lambda: thread.join(1))


@pytest.mark.parametrize("role", ["connector", "all", "api", "worker"])
async def test_free_transfer_stops_owned_clients_but_only_connector_owner_can_complete(
    active_connectors: ActiveConnectors, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    # Given real task/thread runtimes held by all five singleton managers.
    monkeypatch.setattr(get_settings(), "PROCESS_ROLE", role)
    # When free cleanup uses the established per-agent stop APIs.
    with anyio.fail_after(2):
        outcome = await connectors.stop_free_transfer_connectors(active_connectors.agent_id)
    # Then every local runtime is stopped, without mistaking API/worker state for remote shutdown.
    assert all(task.done() for task in active_connectors.tasks)
    assert not active_connectors.thread.is_alive()
    assert outcome == (None if role in ("connector", "all") else "connector_cleanup_pending")


async def test_dingtalk_thread_that_outlives_stop_keeps_cleanup_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a DingTalk runtime that does not exit within its established bounded join.
    agent_id = uuid4()
    release = threading.Event()
    thread = threading.Thread(target=release.wait)
    thread.start()
    join = thread.join
    monkeypatch.setattr(thread, "join", lambda _timeout: join(0))
    monkeypatch.setattr(dingtalk_stream_manager, "_threads", {agent_id: thread})
    monkeypatch.setattr(dingtalk_stream_manager, "_stop_events", {agent_id: threading.Event()})
    monkeypatch.setattr(get_settings(), "PROCESS_ROLE", "connector")

    async def disconnected(_id: UUID) -> None:
        return None

    for manager in (feishu_ws_manager, wecom_stream_manager, wechat_poll_manager, discord_gateway_manager):
        monkeypatch.setattr(manager, "stop_client", disconnected)
    try:
        # When stop_client returns with that thread still alive.
        outcome = await connectors.stop_free_transfer_connectors(agent_id)
        # Then completion is deferred instead of scrubbing while the client still runs.
        assert outcome == "connector_still_running"
    finally:
        release.set()
        await anyio.to_thread.run_sync(lambda: join(1))
