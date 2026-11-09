import asyncio
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import anyio
import pytest
from lark_oapi.ws.client import Client
from lark_oapi.ws.pb.pbbp2_pb2 import Frame

from app.api import feishu
from app.dao.channel_config_dao import channel_config_dao
from app.services.channels import feishu_submissions
from app.services.channels.feishu_ws_acceptance import accept_event_frame
from app.services.channels.stream_source import stream_source
from app.services.feishu_ws import FeishuWSManager
from stream_source_fixtures import connection_config


def event_frame(payload, *, total=1, sequence=0, kind="event"):
    frame = Frame(SeqID=0, LogID=0, service=1, method=1, payload=payload)
    headers = {"message_id": "native-id", "trace_id": "trace", "sum": str(total), "seq": str(sequence), "type": kind}
    for key, value in headers.items():
        header = frame.headers.add()
        header.key, header.value = key, value
    return frame


@pytest.mark.parametrize("fails", [False, True])
@pytest.mark.parametrize("multipart", [False, True])
async def test_sdk_frame_ack_waits_for_native_acceptance(monkeypatch, fails, multipart):
    # Given
    manager = FeishuWSManager()
    started, release = anyio.Event(), anyio.Event()
    acknowledgements, committed = [], []
    agent_id = uuid4()
    config = connection_config(monkeypatch, agent_id, "feishu")
    payload = json.dumps(
        {"header": {"event_type": "im.message.receive_v1"}, "event": {"message": {"message_id": "native-id"}}}
    ).encode()

    async def accept(agent, body, *, source):
        assert agent == agent_id
        assert source == stream_source(config)
        started.set()
        await release.wait()
        if fails:
            raise RuntimeError("database unavailable")
        committed.append(body["event"]["message"]["message_id"])
        return {"code": 0}

    async def write(data):
        response = Frame()
        response.ParseFromString(data)
        acknowledgements.append(json.loads(response.payload))

    client = Client("app", "secret")
    monkeypatch.setattr(client, "_write_message", write)
    monkeypatch.setattr(feishu, "process_feishu_event", accept)

    async def receive(body):
        await manager._async_handle_message(stream_source(config), body)

    if multipart:
        split = len(payload) // 2
        await accept_event_frame(client, event_frame(payload[:split], total=2, sequence=0), receive)
        assert acknowledgements == []
        frame = event_frame(payload[split:], total=2, sequence=1)
    else:
        frame = event_frame(payload)
    # When
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(accept_event_frame, client, frame, receive)
        with anyio.fail_after(1):
            await started.wait()
        assert acknowledgements == committed == []
        release.set()
    # Then
    assert committed == ([] if fails else ["native-id"])
    assert acknowledgements == [{"code": 500 if fails else 200}]


async def test_manager_installs_awaited_handler_on_sdk_data_frames(monkeypatch):
    # Given
    manager = FeishuWSManager()
    client = Client("app", "secret")
    connected = anyio.Event()
    agent_id = uuid4()
    connection_config(monkeypatch, agent_id, "feishu")

    async def connect():
        connected.set()

    async def ping():
        await anyio.sleep_forever()

    async def disconnect():
        return None

    monkeypatch.setattr("app.services.feishu_ws._construct_ws_client", lambda *args, **kwargs: client)
    monkeypatch.setattr(client, "_connect", connect)
    monkeypatch.setattr(client, "_ping_loop", ping)
    monkeypatch.setattr(client, "_disconnect", disconnect)
    received = []

    async def accept(agent, body):
        received.append(body)

    monkeypatch.setattr(manager, "_async_handle_message", accept)
    acknowledgements = []

    async def write(data):
        acknowledgements.append(data)

    monkeypatch.setattr(client, "_write_message", write)
    # When
    try:
        await manager.start_client(agent_id, "app", "secret")
        await connected.wait()
        body = {"header": {"event_type": "im.message.receive_v1"}, "event": {}}
        await client._handle_message(event_frame(json.dumps(body).encode()).SerializeToString())
        # Then
        assert received == [body]
        assert len(acknowledgements) == 1
    finally:
        await manager.stop_all()


@pytest.fixture
async def live_frame_adapter(monkeypatch):
    manager, agent_id, clients = FeishuWSManager(), uuid4(), []
    connected = asyncio.Event()
    connection_config(monkeypatch, agent_id, "feishu")

    def construct(*args, **kwargs):
        client = Client("app", "secret")

        async def connect():
            connected.set()

        async def ping():
            await asyncio.Event().wait()

        async def disconnect():
            return None

        monkeypatch.setattr(client, "_connect", connect)
        monkeypatch.setattr(client, "_ping_loop", ping)
        monkeypatch.setattr(client, "_disconnect", disconnect)
        clients.append(client)
        return client

    monkeypatch.setattr("app.services.feishu_ws._construct_ws_client", construct)
    await manager.start_client(agent_id, "app", "secret")
    await connected.wait()
    try:
        yield manager, agent_id, clients, connected
    finally:
        await manager.stop_all()


@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
async def test_actual_frame_burst_refuses_before_enrichment_and_restarts_fresh(
    monkeypatch, live_frame_adapter, outcome
):
    manager, agent_id, clients, connected = live_frame_adapter
    client = clients[-1]
    started, release, refused = asyncio.Event(), asyncio.Event(), asyncio.Event()
    entered, committed, acknowledgements = [], [], []
    failing = outcome == "failure"

    async def enrich(agent, body, *, source):
        entered.append(body)
        if len(entered) == 8:
            started.set()
        await release.wait()
        if failing:
            raise RuntimeError("database unavailable")
        committed.append(body)

    async def write(data):
        frame = Frame()
        frame.ParseFromString(data)
        acknowledgements.append(json.loads(frame.payload)["code"])
        if len(acknowledgements) == 24:
            refused.set()

    monkeypatch.setattr(feishu, "process_feishu_event", enrich)
    monkeypatch.setattr(client, "_write_message", write)
    payload = json.dumps({"header": {"event_type": "im.message.receive_v1"}, "event": {}}).encode()
    callbacks = [
        asyncio.create_task(client._handle_message(event_frame(payload).SerializeToString())) for _ in range(32)
    ]
    try:
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.wait_for(refused.wait(), 1)
        assert len(entered) == 8
        assert committed == []
        assert acknowledgements == [500] * 24
        await client._handle_message(event_frame(b"{}", kind="card").SerializeToString())
        await client._handle_message(event_frame(payload[:10], total=2).SerializeToString())
        assert len(entered) == 8
        if outcome == "cancel":
            await manager.stop_client(agent_id)
        else:
            release.set()
        results = await asyncio.gather(*callbacks, return_exceptions=True)
        assert len(committed) == (8 if outcome == "success" else 0)
        if outcome == "cancel":
            assert sum(isinstance(result, asyncio.CancelledError) for result in results) == 8
            assert acknowledgements == [500] * 24
        else:
            assert acknowledgements.count(200) == (8 if outcome == "success" else 0)
        await manager.stop_client(agent_id)
        connected.clear()
        await manager.start_client(agent_id, "app", "secret")
        await connected.wait()
        failing = False
        release.set()
        monkeypatch.setattr(clients[-1], "_write_message", write)
        with pytest.raises(asyncio.CancelledError):
            await client._handle_message(event_frame(payload).SerializeToString())
        await clients[-1]._handle_message(event_frame(payload).SerializeToString())
        assert len(entered) == 9
        assert acknowledgements[-1] == 200
    finally:
        for callback in callbacks:
            callback.cancel()
        await asyncio.gather(*callbacks, return_exceptions=True)


async def test_nested_actual_frame_ownership_joins_blocked_ack_write(monkeypatch, live_frame_adapter):
    manager, agent_id, clients, _ = live_frame_adapter
    writing, cleaned = asyncio.Event(), asyncio.Event()

    async def accept(agent, body, *, source):
        return None

    async def write(data):
        writing.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    monkeypatch.setattr(feishu, "process_feishu_event", accept)
    monkeypatch.setattr(clients[-1], "_write_message", write)
    payload = json.dumps({"header": {"event_type": "im.message.receive_v1"}, "event": {}}).encode()
    callback = asyncio.create_task(clients[-1]._handle_message(event_frame(payload).SerializeToString()))
    try:
        await asyncio.wait_for(writing.wait(), 1)
        await manager.stop_client(agent_id)
        assert callback.cancelled()
        assert cleaned.is_set()
    finally:
        callback.cancel()
        await asyncio.gather(callback, return_exceptions=True)


@pytest.mark.parametrize("mutation", ["secret", "mode", "recreated"])
async def test_live_sdk_frames_reject_revoked_sources_without_waiting_for_shutdown(
    monkeypatch, live_frame_adapter, mutation
):
    manager, agent_id, clients, _ = live_frame_adapter
    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type=feishu.ChannelType.FEISHU)
    assert config is not None
    original = stream_source(config)
    accept = AsyncMock(return_value=True)
    monkeypatch.setattr(feishu_submissions, "accept_feishu_event", accept)
    acknowledgements = []

    async def write(data):
        frame = Frame()
        frame.ParseFromString(data)
        acknowledgements.append(json.loads(frame.payload)["code"])

    client = clients[-1]
    monkeypatch.setattr(client, "_write_message", write)
    payload = json.dumps({"header": {"event_type": "im.message.receive_v1"}, "event": {}}).encode()
    await client._handle_message(event_frame(payload).SerializeToString())
    assert accept.await_args is not None
    assert accept.await_args.kwargs["source"] == original
    match mutation:
        case "secret":
            config.app_secret = "rotated"
        case "mode":
            config.extra_config = {"connection_mode": "webhook"}
        case "recreated":
            config.id = uuid4()
    await client._handle_message(event_frame(payload).SerializeToString())
    assert acknowledgements == [200, 500]
    accept.assert_awaited_once()
    assert not manager._tasks[agent_id].done()
