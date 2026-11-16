import base64
import hashlib

import anyio
import pytest
from pydantic import JsonValue, ValidationError

from app.services.channels.zalo_personal_media_transport import MediaTransferError, MediaTransport
from app.services.channels.zalo_personal_protocol import MediaChunk, MediaEnd, MediaOffer, Message, child_frame


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize(("fault", "code"), [("sequence", "invalid"), ("digest", "digest_mismatch")])
async def test_download_integrity_failure_cancels_with_bounded_code_and_releases_transfer(fault, code):
    frames = []
    transport: MediaTransport

    async def write(frame: dict[str, JsonValue]) -> None:
        frames.append(frame)
        if frame["type"] != "media_get":
            return
        transfer = str(frame["transfer_id"])
        await transport.handle(
            MediaChunk(
                type="media_chunk",
                id="chunk",
                token="t",
                transfer_id=transfer,
                seq=1 if fault == "sequence" else 0,
                data=base64.b64encode(b"abc").decode(),
            )
        )
        if fault == "digest":
            await transport.handle(
                MediaEnd(
                    type="media_end",
                    id="end",
                    token="t",
                    transfer_id=transfer,
                    size=3,
                    sha256=hashlib.sha256(b"wrong").hexdigest(),
                )
            )

    transport = MediaTransport(write, "t")
    frame = Message(
        type="message",
        id="m",
        token="t",
        message_id="m",
        thread_id="2",
        sender_id="2",
        from_me=False,
        timestamp=1,
        text="",
        media=MediaOffer(source_id="a" * 64, kind="file", filename="a.bin"),
    )
    with pytest.raises(MediaTransferError, match=code):
        await transport.download(frame)
    assert frames[-1]["type"] == "media_error"
    assert frames[-1]["code"] == code
    assert transport.transfers == {}
    transport.close()


@pytest.mark.anyio
async def test_cancelled_media_wait_does_not_keep_a_live_transfer():
    started = anyio.Event()
    frames = []

    async def write(frame: dict[str, JsonValue]) -> None:
        frames.append(frame)
        started.set()

    transport = MediaTransport(write, "t")
    async with anyio.create_task_group() as tasks:

        async def wait_for_media():
            async with transport.open() as transfer:
                await transport.emit("media_get", transfer.id, message_id="m", source_id="a" * 64, limit=1)
                await transfer.next()

        tasks.start_soon(wait_for_media)
        await started.wait()
        tasks.cancel_scope.cancel()
    assert transport.transfers == {}
    assert frames[-1]["type"] == "media_error"
    assert frames[-1]["code"] == "expired"
    transport.close()


@pytest.mark.parametrize("data", ["AAAA=", "AA", "____", base64.b64encode(bytes(65537)).decode()])
def test_python_child_contract_refuses_noncanonical_or_oversized_chunks(data):
    with pytest.raises(ValidationError):
        child_frame.validate_python(
            {"id": "i", "token": "t", "type": "media_chunk", "transfer_id": "t", "seq": 0, "data": data}
        )
