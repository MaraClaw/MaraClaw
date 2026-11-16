import base64
import hashlib
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Final, assert_never
from uuid import uuid4

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from pydantic import JsonValue

from app.records.channel_media import StoredFile
from app.services.channels.zalo_personal_protocol import (
    MediaAck,
    MediaChunk,
    MediaCode,
    MediaEnd,
    MediaError,
    MediaReady,
    Message,
    TransferFrame,
)

CHUNK_BYTES: Final = 65536
TRANSFER_SECONDS: Final = 120
FILE_BYTES: Final = 50 * 1024 * 1024
IMAGE_BYTES: Final = 10 * 1024 * 1024
type Write = Callable[[dict[str, JsonValue]], Awaitable[None]]


class MediaTransferError(Exception):
    def __init__(self, code: MediaCode) -> None:
        self.code: MediaCode = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class Transfer:
    id: str
    send: MemoryObjectSendStream[TransferFrame]
    receive: MemoryObjectReceiveStream[TransferFrame]

    async def next(self) -> MediaChunk | MediaEnd | MediaAck | MediaReady:
        frame = await self.receive.receive()
        match frame:
            case MediaError():
                raise MediaTransferError(frame.code)
            case MediaChunk() | MediaEnd() | MediaAck() | MediaReady():
                return frame
            case unreachable:
                assert_never(unreachable)


class MediaTransport:
    def __init__(self, write: Write, token: str) -> None:
        self.write: Write = write
        self.token: str = token
        self.transfers: dict[str, Transfer] = {}
        self.capacity: anyio.CapacityLimiter = anyio.CapacityLimiter(2)

    async def emit(self, kind: str, transfer_id: str, **fields: JsonValue) -> None:
        await self.write({"id": uuid4().hex, "token": self.token, "type": kind, "transfer_id": transfer_id, **fields})

    async def handle(self, frame: TransferFrame) -> None:
        transfer = self.transfers.get(frame.transfer_id)
        if transfer is None:
            if frame.type != "media_error":
                await self.emit("media_error", frame.transfer_id, code="expired")
            return
        try:
            transfer.send.send_nowait(frame)
        except anyio.WouldBlock:
            raise MediaTransferError("invalid") from None

    @asynccontextmanager
    async def open(self) -> AsyncGenerator[Transfer]:
        try:
            with anyio.fail_after(TRANSFER_SECONDS):
                async with self.capacity:
                    send, receive = anyio.create_memory_object_stream[TransferFrame](2)
                    transfer = Transfer(uuid4().hex, send, receive)
                    self.transfers[transfer.id] = transfer
                    code: MediaCode = "expired"
                    try:
                        yield transfer
                    except MediaTransferError as error:
                        code = error.code
                        raise
                    finally:
                        self.transfers.pop(transfer.id, None)
                        send.close()
                        receive.close()
                        with anyio.move_on_after(1, shield=True):
                            await self.emit("media_error", transfer.id, code=code)
        except TimeoutError:
            raise MediaTransferError("expired") from None

    async def download(self, message: Message) -> bytes:
        media = message.media
        if media is None:
            raise MediaTransferError("invalid")
        limit = IMAGE_BYTES if media.kind == "image" else FILE_BYTES
        async with self.open() as transfer:
            await self.emit(
                "media_get", transfer.id, message_id=message.message_id, source_id=media.source_id, limit=limit
            )
            data = bytearray()
            seq = 0
            while True:
                frame = await transfer.next()
                match frame:
                    case MediaChunk():
                        chunk = base64.b64decode(frame.data, validate=True)
                        if frame.seq != seq:
                            raise MediaTransferError("invalid")
                        if len(data) + len(chunk) > limit:
                            raise MediaTransferError("too_large")
                        data.extend(chunk)
                        seq += 1
                        await self.emit("media_ack", transfer.id, seq=frame.seq)
                    case MediaEnd():
                        if len(data) != frame.size or hashlib.sha256(data).hexdigest() != frame.sha256:
                            raise MediaTransferError("digest_mismatch")
                        return bytes(data)
                    case MediaAck() | MediaReady():
                        raise MediaTransferError("invalid")
                    case unreachable:
                        assert_never(unreachable)

    @asynccontextmanager
    async def stage(self, file: StoredFile, data: bytes) -> AsyncGenerator[str]:
        if len(data) != file.size or not data or hashlib.sha256(data).hexdigest() != file.sha256:
            raise MediaTransferError("digest_mismatch")
        if len(data) > (IMAGE_BYTES if file.kind == "image" else FILE_BYTES):
            raise MediaTransferError("too_large")
        async with self.open() as transfer:
            await self.emit("media_put", transfer.id, size=file.size, sha256=file.sha256)
            ready = await transfer.next()
            if not isinstance(ready, MediaReady) or ready.phase != "receiving":
                raise MediaTransferError("invalid")
            for seq, offset in enumerate(range(0, len(data), CHUNK_BYTES)):
                await self.emit(
                    "media_chunk",
                    transfer.id,
                    seq=seq,
                    data=base64.b64encode(data[offset : offset + CHUNK_BYTES]).decode("ascii"),
                )
                ack = await transfer.next()
                if not isinstance(ack, MediaAck) or ack.seq != seq:
                    raise MediaTransferError("invalid")
            await self.emit("media_end", transfer.id, size=file.size, sha256=file.sha256)
            complete = await transfer.next()
            if not isinstance(complete, MediaReady) or complete.phase != "complete":
                raise MediaTransferError("invalid")
            yield transfer.id

    def close(self) -> None:
        for transfer in self.transfers.values():
            transfer.send.close()
