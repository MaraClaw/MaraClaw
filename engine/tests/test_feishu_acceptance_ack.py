import json
from uuid import uuid4

import anyio
import pytest
from lark_oapi.ws.client import Client
from lark_oapi.ws.pb.pbbp2_pb2 import Frame

from app.api import feishu
from app.services.channels.feishu_ws_acceptance import accept_event_frame
from app.services.feishu_ws import FeishuWSManager


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
    payload = json.dumps(
        {"header": {"event_type": "im.message.receive_v1"}, "event": {"message": {"message_id": "native-id"}}}
    ).encode()

    async def accept(agent, body):
        assert agent == agent_id
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
        await manager._async_handle_message(agent_id, body)

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
        await manager.start_client(uuid4(), "app", "secret")
        await connected.wait()
        body = {"header": {"event_type": "im.message.receive_v1"}, "event": {}}
        await client._handle_message(event_frame(json.dumps(body).encode()).SerializeToString())
        # Then
        assert received == [body]
        assert len(acknowledgements) == 1
    finally:
        await manager.stop_all()
