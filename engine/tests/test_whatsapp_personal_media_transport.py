import base64
import hashlib
from contextlib import contextmanager
from dataclasses import dataclass
from tempfile import TemporaryFile
from typing import BinaryIO

import anyio
import pytest
from pydantic import JsonValue

from app.services.channels import whatsapp_personal_media_transport as module
from app.services.channels.whatsapp_personal_media_transport import MediaTransferError, MediaTransport
from app.services.channels.whatsapp_personal_protocol import MediaChunk, MediaEnd, MediaOffer, Message


def message(size: int = 0) -> Message:
    return Message(
        type="message",
        id="original",
        token="capability",
        message_id="native",
        remote_jid="self@s.whatsapp.net",
        participant=None,
        from_me=True,
        timestamp=1,
        text="",
        event_kind="notify",
        is_history=False,
        media=MediaOffer(
            source_id="a" * 64, kind="file", filename="report.docx", content_type="application/octet-stream", size=size
        ),
    )


@dataclass(slots=True)
class Result:
    data: bytes | None = None
    error: str | None = None


@pytest.fixture
def spools(monkeypatch):
    opened: list[BinaryIO] = []

    @contextmanager
    def temporary_file(*args, **kwargs):
        with TemporaryFile(*args, **kwargs) as handle:
            opened.append(handle)
            yield handle

    monkeypatch.setattr(module.tempfile, "TemporaryFile", temporary_file)
    return opened


@pytest.mark.parametrize(
    "failure", [None, "digest", "size", "declared_size", "oversize", "sequence", "window", "revoked", "timeout"]
)
async def test_parent_spools_only_verified_bounded_chunks_and_releases_on_every_terminal_path(
    failure, spools, monkeypatch
):
    send, receive = anyio.create_memory_object_stream[dict[str, JsonValue]](8)
    transport = MediaTransport(send.send, "capability")
    result = Result()
    data = b"first" + b"second"
    if failure == "timeout":
        monkeypatch.setattr(module, "TRANSFER_SECONDS", 0.05)

    async def download():
        try:
            result.data = await transport.download(
                message(1 if failure == "declared_size" else 0), 5 if failure == "oversize" else 100
            )
        except MediaTransferError as error:
            result.error = error.code

    async with send, receive, anyio.create_task_group() as tasks:
        tasks.start_soon(download)
        start = await receive.receive()
        transfer_id = start["transfer_id"]
        assert isinstance(transfer_id, str)
        if failure == "timeout":
            pass
        elif failure == "revoked":
            transport.close()
        elif failure == "sequence":
            with pytest.raises(MediaTransferError, match="invalid"):
                await transport.handle(
                    MediaChunk(
                        type="media_chunk", id="bad", token="capability", transfer_id=transfer_id, seq=1, data="YQ=="
                    )
                )
            transport.close()
        elif failure == "window":
            first = MediaChunk(
                type="media_chunk", id="first", token="capability", transfer_id=transfer_id, seq=0, data="YQ=="
            )
            await transport.handle(first)
            with pytest.raises(MediaTransferError, match="invalid"):
                await transport.handle(first.model_copy(update={"seq": 1}))
            transport.close()
        else:
            for seq, part in enumerate((data[:5], data[5:])):
                await transport.handle(
                    MediaChunk(
                        type="media_chunk",
                        id=f"chunk-{seq}",
                        token="capability",
                        transfer_id=transfer_id,
                        seq=seq,
                        data=base64.b64encode(part).decode(),
                    )
                )
                reply = await receive.receive()
                if reply["type"] == "media_error":
                    break
                assert (reply["type"], reply["seq"]) == ("media_ack", seq)
            if failure != "oversize":
                await transport.handle(
                    MediaEnd(
                        type="media_end",
                        id="end",
                        token="capability",
                        transfer_id=transfer_id,
                        seq=2,
                        size=1 if failure == "size" else len(data),
                        sha256="a" * 64 if failure == "digest" else hashlib.sha256(data).hexdigest(),
                    )
                )
    if failure is None:
        assert result.data == data
        assert result.error is None
    else:
        assert result.data is None
        assert result.error == (
            "too_large"
            if failure == "oversize"
            else "digest_mismatch"
            if failure in {"digest", "size", "declared_size"}
            else "expired"
        )
    assert transport.transfer is None
    assert spools
    assert all(spool.closed for spool in spools)
