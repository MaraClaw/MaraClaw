import asyncio
import json
from contextlib import suppress
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import wecom_aibot_sdk
from fastapi import HTTPException
from wecom_aibot_sdk import WSClient

from app.services import wecom_stream
from stream_source_fixtures import connection_config


class QueueSocket:
    def __init__(self):
        self.incoming: asyncio.Queue[str] = asyncio.Queue()

    async def recv(self):
        return await self.incoming.get()

    async def close(self):
        return None


@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
async def test_actual_sdk_receive_loop_keeps_acks_live_during_a_bounded_message_burst(monkeypatch, outcome):
    manager, agent_id = wecom_stream.WeComStreamManager(), uuid4()
    connection_config(monkeypatch, agent_id, "wecom")
    clients, entered, committed, replies, acks = [], [], [], [], []
    connected, started, refused_acks, release = (asyncio.Event() for _ in range(4))
    reply_started, reply_release, dropped = (asyncio.Event() for _ in range(3))
    finished = []
    failing = outcome == "failure"

    def construct(options):
        client = WSClient(options)
        socket = QueueSocket()
        ws = client._ws_manager

        async def connect():
            monkeypatch.setattr(ws, "_ws", socket)
            ws._connected = True
            ws._missed_pong_count = 1
            ws._start_background_tasks()
            connected.set()

        async def send(frame):
            replies.append(frame)
            if "capacity is full" in frame["body"]["stream"]["content"]:
                reply_started.set()
                await reply_release.wait()
            await socket.incoming.put(json.dumps({"headers": frame["headers"], "errcode": 0}))

        original_ack = ws._handle_reply_ack

        def ack(req_id, frame):
            original_ack(req_id, frame)
            acks.append(req_id)
            refused_acks.set()

        original_run = client._message_handler._run_handler

        async def run(handler, event, frame):
            try:
                await original_run(handler, event, frame)
            finally:
                finished.append(frame)
                if len(finished) == 23:
                    dropped.set()

        monkeypatch.setattr(client, "connect_async", connect)
        monkeypatch.setattr(ws, "_send_raw", send)
        monkeypatch.setattr(ws, "_handle_reply_ack", ack)
        monkeypatch.setattr(client._message_handler, "_run_handler", run)
        clients.append((client, socket))
        return client

    async def process(**kwargs):
        entered.append(kwargs)
        if len(entered) == 8:
            started.set()
        await release.wait()
        if failing:
            raise RuntimeError("database unavailable")
        committed.append(kwargs)
        return "accepted"

    def message(index, kind="text"):
        return json.dumps(
            {
                "headers": {"req_id": f"request-{index}"},
                "body": {
                    "msgtype": kind,
                    "msgid": f"message-{index}",
                    "from_userid": "sender",
                    "text": {"content": "hello"},
                },
            }
        )

    monkeypatch.setattr(wecom_aibot_sdk, "WSClient", construct)
    monkeypatch.setattr(wecom_stream, "_disable_wecom_sdk_proxy", lambda: None)
    monkeypatch.setattr(wecom_stream, "_process_wecom_stream_message", process)
    try:
        await manager.start_client(agent_id, "bot", "secret")
        await asyncio.wait_for(connected.wait(), 1)
        client, socket = clients[-1]
        await socket.incoming.put(json.dumps({"headers": {"req_id": "ping-test"}, "errcode": 0}))
        for index in range(32):
            await socket.incoming.put(message(index, "file" if index == 31 else "text"))
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.wait_for(reply_started.wait(), 1)
        await asyncio.wait_for(dropped.wait(), 1)
        assert len(entered) == 8
        assert len(manager._callbacks[agent_id].tasks) == 9
        assert committed == []
        assert client._ws_manager._missed_pong_count == 0
        assert all("capacity is full" in frame["body"]["stream"]["content"] for frame in replies)
        assert len(replies) == 1
        reply_release.set()
        await asyncio.wait_for(refused_acks.wait(), 2)
        old_handler = client._message_handler._event_handlers["message.text"][0]
        if outcome == "cancel":
            await manager.stop_client(agent_id)
        else:
            release.set()
            await asyncio.wait_for(asyncio.gather(*tuple(manager._callbacks[agent_id].tasks)), 2)
        assert len(committed) == (8 if outcome == "success" else 0)
        await manager.stop_client(agent_id)
        assert not client._ws_manager._pending_acks
        connected.clear()
        failing = False
        release.set()
        await manager.start_client(agent_id, "bot", "secret")
        await asyncio.wait_for(connected.wait(), 1)
        fresh, _ = clients[-1]
        frame = fresh._message_handler.parse_frame(json.loads(message("fresh")))
        with pytest.raises(asyncio.CancelledError):
            await old_handler(frame)
        await asyncio.wait_for(fresh._message_handler._event_handlers["message.text"][0](frame), 2)
        assert len(entered) == 9
    finally:
        reply_release.set()
        release.set()
        await manager.stop_all()
        for client, _ in clients:
            receive_task = client._ws_manager._receive_task
            assert receive_task is not None
            assert receive_task.done()


@pytest.mark.parametrize("mutation", ["secret", "mode", "recreated", "during_processing"])
async def test_live_connection_revocation_blocks_text_media_welcome_and_error_replies(monkeypatch, mutation):
    manager, agent_id = wecom_stream.WeComStreamManager(), uuid4()
    config = connection_config(monkeypatch, agent_id, "wecom")
    connected = asyncio.Event()
    clients = []
    reply, welcome, process = AsyncMock(), AsyncMock(), AsyncMock(return_value="accepted")

    def construct(options):
        client = WSClient(options)

        async def connect():
            connected.set()

        monkeypatch.setattr(client, "connect_async", connect)
        clients.append(client)
        return client

    async def revoke_while_processing(**kwargs):
        assert kwargs["source"].config_id == config.id
        config.extra_config = {**config.extra_config, "bot_secret": "rotated"}
        raise RuntimeError("processing interrupted")

    monkeypatch.setattr(wecom_aibot_sdk, "WSClient", construct)
    monkeypatch.setattr(wecom_stream, "_disable_wecom_sdk_proxy", lambda: None)
    monkeypatch.setattr(wecom_stream, "_reply_stream", reply)
    monkeypatch.setattr(wecom_stream, "_reply_welcome", welcome)
    monkeypatch.setattr(wecom_stream, "_process_wecom_stream_message", process)
    try:
        await manager.start_client(agent_id, "bot", "secret")
        await asyncio.wait_for(connected.wait(), 1)
        client = clients[-1]
        match mutation:
            case "secret":
                config.extra_config = {**config.extra_config, "bot_secret": "rotated"}
            case "mode":
                config.extra_config = {"connection_mode": "webhook"}
            case "recreated":
                config.id = uuid4()
            case "during_processing":
                monkeypatch.setattr(wecom_stream, "_process_wecom_stream_message", revoke_while_processing)
        for name in ("message.text", "message.image", "message.file", "event.enter_chat"):
            frame = client._message_handler.parse_frame(
                {
                    "headers": {"req_id": name},
                    "body": {
                        "msgtype": "text",
                        "msgid": "native-1",
                        "from_userid": "sender",
                        "text": {"content": "hello"},
                    },
                }
            )
            with suppress(HTTPException):
                await client._message_handler._event_handlers[name][0](frame)
        reply.assert_not_awaited()
        welcome.assert_not_awaited()
        process.assert_not_awaited()
        assert not manager._tasks[agent_id].done()
    finally:
        await manager.stop_all()
