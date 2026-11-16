import hashlib
import json
import shutil
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import anyio
import pytest
from pydantic import JsonValue

from app.records.channel_media import StoredFile
from app.records.zalo_personal import ZaloConnection, ZaloMediaSend
from app.services.channels import (
    zalo_personal_child as child_module,
    zalo_personal_events as events,
    zalo_personal_intake as intake_module,
    zalo_personal_journal as journal,
)
from app.services.channels.zalo_personal_child import ZaloChild, spawn_child
from app.services.channels.zalo_personal_media_transport import MediaTransferError
from app.services.channels.zalo_personal_protocol import CredsSet, Heartbeat, MediaOffer, Message, SendResult


@pytest.fixture
def anyio_backend():
    return "asyncio"


def connection() -> ZaloConnection:
    return ZaloConnection(
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        1,
        "connected",
        True,
        datetime.now(UTC),
        claim_token=uuid4(),
        account_id="10001",
    )


@pytest.mark.anyio
async def test_real_node_media_pipe_demultiplexes_control_and_authorizes_only_after_staging(monkeypatch):
    node = shutil.which("node")
    assert node is not None
    fixture = Path(__file__).parents[1] / "connectors/zalo/dist/test/media_child.js"
    assert fixture.is_file(), "Build the Zalo bridge before running its controlled-child tests"
    data = bytes([37]) * (1024 * 1024 + 333)
    child = ZaloChild(connection())
    controls: list[str] = []
    accepted = anyio.Event()
    downloaded = anyio.Event()
    ready = anyio.Event()
    observed: list[dict[str, JsonValue]] = []
    command_id = uuid4()
    path = "workspace/uploads/zalo/replies/owned/file.bin"
    file = StoredFile(
        storage_key=f"{child.connection.agent_id}/{path}",
        path=path,
        name="file.bin",
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        kind="file",
    )

    async def intake(current, frame, *, download=None):
        assert current == child.connection
        assert download is not None
        if frame.message_id == "blocked":
            return
        assert frame.message_id == "allowed"
        assert await download(frame) == data
        downloaded.set()

    async def handle(current, frame):
        assert current == child.connection
        match frame:
            case CredsSet():
                controls.append("creds_set")
            case Heartbeat():
                assert frame.ok
                controls.append("heartbeat")
            case SendResult():
                assert frame.outcome == "accepted"
                assert frame.message_id == "90001"
                accepted.set()
            case _:
                if frame.type == "ready":
                    ready.set()

    async def authorize(current, selected: UUID):
        assert current == child.connection
        assert selected == command_id
        ends = [frame for frame in observed if frame["type"] == "media_end"]
        assert len(ends) == 1
        assert ends[0]["sha256"] == file.sha256
        return ZaloMediaSend(
            command_id=selected,
            config_id=current.config_id,
            auth_epoch=current.auth_epoch,
            generation=current.generation,
            account_id="10001",
            peer_id="20002",
            file=file,
            state="dispatch_started",
            claim_token=current.claim_token,
            created_at=current.created_at,
        )

    async def record(*_args):
        pytest.fail("A proven attachment acknowledgment cannot become uncertain")

    original_write = child.write

    async def write(frame):
        assert len(json.dumps(frame).encode()) < 1048576
        observed.append(frame)
        await original_write(frame)

    monkeypatch.setattr(child, "write", write)
    child.media.write = write
    monkeypatch.setattr(child_module, "handle_frame", handle)
    monkeypatch.setattr(intake_module, "intake", intake)
    monkeypatch.setattr(journal, "authorize_media_dispatch", authorize, raising=False)
    monkeypatch.setattr(journal, "record_result", record)
    child.process = await spawn_child([node, str(fixture)])
    try:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(child.read)
            await child.write(
                {
                    "id": "init",
                    "token": child.token,
                    "type": "init",
                    "lease_ms": 120000,
                    "credentials": json.dumps(
                        {
                            "cookie": [{"key": "s", "value": "private", "domain": "zalo.me"}],
                            "imei": "test",
                            "userAgent": "test",
                        }
                    ),
                    "keepalive_interval_ms": 60000,
                    "media_allowed_hosts": ["media.example"],
                    "media_transfer_timeout_ms": 120000,
                }
            )
            with anyio.fail_after(10):
                await ready.wait()
                await child.write({"id": "lease", "token": child.token, "type": "lease", "lease_ms": 120000})
                await downloaded.wait()
                await child.send_file(str(command_id), "20002", file, data)
                await accepted.wait()
            assert controls == ["creds_set", "creds_set", "heartbeat"]
            assert [frame["message_id"] for frame in observed if frame["type"] == "media_get"] == ["allowed"]
            assert any(frame["type"] == "lease" for frame in observed)
            assert len([frame for frame in observed if frame["type"] == "send"]) == 1
            await child.write({"id": "stop", "token": child.token, "type": "shutdown"})
            with anyio.fail_after(3):
                await child.process.wait()
            tasks.cancel_scope.cancel()
    finally:
        child.media.close()
        if child.process.returncode is None:
            child.process.kill()
            await child.process.wait()
        await child.process.aclose()
    assert child.process.returncode == 0


@pytest.mark.anyio
async def test_media_event_does_not_open_an_encompassing_transaction(monkeypatch):
    child = ZaloChild(connection())
    frame = Message(
        type="message",
        id="frame",
        token=child.token,
        message_id="m",
        thread_id="20002",
        sender_id="20002",
        from_me=False,
        timestamp=1,
        text="",
        media=MediaOffer(source_id="a" * 64, kind="file", filename="file.bin"),
    )
    calls = []

    @asynccontextmanager
    async def forbidden_transaction():
        pytest.fail("Media intake may authorize in its own short transactions")
        yield

    async def intake(current, received, *, download=None):
        assert current == child.connection
        assert received == frame
        assert download == child.download_message_media
        calls.append(received.message_id)

    monkeypatch.setattr(events, "connection_ctx", forbidden_transaction)
    monkeypatch.setattr(intake_module, "intake", intake)
    await events.handle_frame(child.connection, frame, download=child.download_message_media)
    assert calls == ["m"]
    with pytest.raises(MediaTransferError, match="invalid"):
        await child.download_message_media(frame.model_copy(update={"media": None}))
