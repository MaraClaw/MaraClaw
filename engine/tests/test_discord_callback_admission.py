import asyncio
from contextlib import asynccontextmanager, suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import discord
import pytest
from fastapi import HTTPException

from app.services.channels import submissions
from stream_source_fixtures import connection_config


@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
async def test_actual_sdk_dispatch_bounds_durable_waiters_before_typing_and_restarts_fresh(monkeypatch, outcome):
    from app.services import discord_gateway

    manager, agent_id = discord_gateway.DiscordGatewayManager(), uuid4()
    connection_config(monkeypatch, agent_id, "discord", credential="token")
    connected, started, refused, release = (asyncio.Event() for _ in range(4))
    clients, entered, committed, typing, replies = [], [], [], [], []
    reply_release = asyncio.Event()
    deliveries, runners = [], []
    failing = outcome == "failure"

    async def start(client, token, *, reconnect):
        await client._async_setup_hook()
        clients.append(client)
        connected.set()
        await asyncio.Event().wait()

    async def accept(event):
        entered.append(event)
        if len(entered) == 8:
            started.set()
        await release.wait()
        if failing:
            raise RuntimeError("database unavailable")
        committed.append(event)

    @asynccontextmanager
    async def show_typing():
        typing.append(True)
        yield

    async def reply(content, *, mention_author):
        replies.append(content)
        refused.set()
        await reply_release.wait()

    def message(index):
        return SimpleNamespace(
            id=index,
            author=SimpleNamespace(id=1, name="sender"),
            guild=None,
            mentions=[],
            content="hello",
            channel=SimpleNamespace(id=2, typing=show_typing),
            reply=reply,
        )

    monkeypatch.setattr(discord_gateway, "_HAS_DISCORD", True)
    monkeypatch.setattr(discord_gateway, "_get_discord_module", lambda: discord)
    monkeypatch.setattr(discord.Client, "start", start)
    monkeypatch.setattr(submissions, "accept_channel_event", accept)
    try:
        await manager.start_client(agent_id, "token")
        await asyncio.wait_for(connected.wait(), 1)
        client = clients[-1]
        runners.append(manager._tasks[agent_id])
        await manager.start_client(agent_id, "token", stop_existing=False)
        assert manager._clients[agent_id] is client
        schedule = client._schedule_event

        def record_schedule(coro, name, *args, **kwargs):
            task = schedule(coro, name, *args, **kwargs)
            deliveries.append(task)
            return task

        monkeypatch.setattr(client, "_schedule_event", record_schedule)
        for index in range(32):
            client.dispatch("message", message(index))
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.wait_for(refused.wait(), 1)
        await asyncio.wait_for(asyncio.gather(*deliveries[9:]), 1)
        assert sum(not task.done() for task in deliveries) == 9
        assert len(entered) == len(typing) == 8
        assert committed == []
        assert all("capacity is full" in content for content in replies)
        assert len(replies) == 1
        reply_release.set()
        await asyncio.wait_for(deliveries[8], 1)
        old_handler = client.on_message
        await client.on_ready()
        if outcome == "cancel":
            await manager.stop_client(agent_id)
        else:
            release.set()
            await asyncio.wait_for(asyncio.gather(*tuple(manager._callbacks[agent_id].tasks)), 1)
        assert len(committed) == (8 if outcome == "success" else 0)
        await manager.stop_client(agent_id)
        assert client.is_closed()
        connected.clear()
        failing = False
        release.set()
        await manager.start_client(agent_id, "token")
        await asyncio.wait_for(connected.wait(), 1)
        with pytest.raises(asyncio.CancelledError):
            await old_handler(message("stale"))
        await clients[-1].on_message(message("fresh"))
        assert len(entered) == len(typing) == 9
    finally:
        reply_release.set()
        release.set()
        await manager.stop_all()
        for runner in runners:
            if not runner.done():
                runner.cancel()
        await asyncio.gather(*runners, return_exceptions=True)
        assert all(client.is_closed() for client in clients)


@pytest.mark.parametrize("mutation", ["secret", "mode", "recreated", "during_processing"])
async def test_connected_gateway_revokes_typing_and_error_replies_before_new_effects(monkeypatch, mutation):
    from app.services import discord_gateway

    manager, agent_id = discord_gateway.DiscordGatewayManager(), uuid4()
    config = connection_config(monkeypatch, agent_id, "discord", credential="token")
    connected = asyncio.Event()
    clients, typing = [], []
    reply = AsyncMock()

    async def start(client, token, *, reconnect):
        await client._async_setup_hook()
        clients.append(client)
        connected.set()
        await asyncio.Event().wait()

    @asynccontextmanager
    async def show_typing():
        typing.append(True)
        yield

    async def interrupted(event):
        config.app_secret = "rotated"
        raise RuntimeError("database interrupted")

    accept = AsyncMock(side_effect=interrupted)
    monkeypatch.setattr(discord_gateway, "_get_discord_module", lambda: discord)
    monkeypatch.setattr(discord.Client, "start", start)
    monkeypatch.setattr(submissions, "accept_channel_event", accept)
    try:
        await manager.start_client(agent_id, "token")
        await asyncio.wait_for(connected.wait(), 1)
        match mutation:
            case "secret":
                config.app_secret = "rotated"
            case "mode":
                config.extra_config = {"connection_mode": "webhook"}
            case "recreated":
                config.id = uuid4()
            case "during_processing":
                pass
        message = SimpleNamespace(
            id=1,
            author=SimpleNamespace(id=1, name="sender"),
            guild=None,
            mentions=[],
            content="hello",
            channel=SimpleNamespace(id=2, typing=show_typing),
            reply=reply,
        )
        with suppress(HTTPException):
            await clients[-1].on_message(message)
        assert typing == ([True] if mutation == "during_processing" else [])
        assert accept.await_count == (1 if mutation == "during_processing" else 0)
        reply.assert_not_awaited()
        assert not manager._tasks[agent_id].done()
    finally:
        await manager.stop_all()
        assert all(client.is_closed() for client in clients)
