import base64
import hashlib
import io
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final, assert_never
from uuid import uuid4

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from pydantic import JsonValue

from app.services.channels.whatsapp_personal_protocol import (
    MediaChunk,
    MediaCode,
    MediaEnd,
    MediaError,
    Message,
    TransferFrame,
)

CHUNK_BYTES: Final = 65536
TRANSFER_SECONDS: Final = 120
type Write = Callable[[dict[str, JsonValue]], Awaitable[None]]


class MediaTransferError(ValueError):
    def __init__(self, code: MediaCode) -> None:
        self.code: MediaCode = code
        super().__init__(code)


@dataclass(slots=True)
class Transfer:
    id: str
    send: MemoryObjectSendStream[TransferFrame]
    receive: MemoryObjectReceiveStream[TransferFrame]
    seq: int = 0
    receiving: bool = True


class MediaTransport:
    def __init__(self, write: Write, token: str) -> None:
        self.write: Write = write
        self.token: str = token
        self.transfer: Transfer | None = None

    async def emit(self, kind: str, transfer_id: str, **fields: JsonValue) -> None:
        await self.write({"id": uuid4().hex, "token": self.token, "type": kind, "transfer_id": transfer_id, **fields})

    async def handle(self, frame: TransferFrame) -> None:
        state = self.transfer
        if state is None or state.id != frame.transfer_id:
            if not isinstance(frame, MediaError):
                await self.emit("media_error", frame.transfer_id, code="expired")
            return
        match frame:
            case MediaChunk() | MediaEnd():
                if not state.receiving or frame.seq != state.seq:
                    raise MediaTransferError("invalid")
                state.receiving = False
                if isinstance(frame, MediaChunk):
                    state.seq += 1
            case MediaError():
                pass
            case unreachable:
                assert_never(unreachable)
        try:
            state.send.send_nowait(frame)
        except anyio.WouldBlock:
            raise MediaTransferError("invalid") from None

    async def download(self, message: Message, limit: int) -> bytes:
        offer = message.media
        if offer is None or self.transfer is not None:
            raise MediaTransferError("invalid")
        send, receive = anyio.create_memory_object_stream[TransferFrame](1)
        state = Transfer(uuid4().hex, send, receive)
        self.transfer = state
        code: MediaCode = "expired"
        completed = False
        try:
            with (
                anyio.fail_after(TRANSFER_SECONDS),
                tempfile.TemporaryFile(mode="w+b", buffering=0) as raw,
                io.BufferedRandom(raw, buffer_size=8192) as spool,
            ):
                await self.emit(
                    "media_get", state.id, message_id=message.message_id, source_id=offer.source_id, limit=limit
                )
                size, seq = 0, 0
                digest = hashlib.sha256()
                while True:
                    frame = await receive.receive()
                    match frame:
                        case MediaChunk():
                            chunk = base64.b64decode(frame.data, validate=True)
                            if frame.seq != seq:
                                raise MediaTransferError("invalid")
                            if len(chunk) > limit - size:
                                raise MediaTransferError("too_large")
                            size += len(chunk)
                            digest.update(chunk)
                            _ = await anyio.to_thread.run_sync(spool.write, chunk)
                            del chunk
                            seq += 1
                            state.receiving = True
                            await self.emit("media_ack", state.id, seq=frame.seq)
                        case MediaEnd():
                            if frame.seq != seq or size != frame.size or digest.hexdigest() != frame.sha256:
                                raise MediaTransferError("digest_mismatch")
                            if size == 0 or (offer.size and size != offer.size):
                                raise MediaTransferError("digest_mismatch")
                            _ = spool.seek(0)
                            data = await anyio.to_thread.run_sync(spool.read, size)
                            completed = True
                            return data
                        case MediaError():
                            raise MediaTransferError(frame.code)
                        case unreachable:
                            assert_never(unreachable)
        except TimeoutError:
            raise MediaTransferError("expired") from None
        except MediaTransferError as error:
            code = error.code
            raise
        except anyio.EndOfStream:
            raise MediaTransferError("expired") from None
        finally:
            self.transfer = None
            send.close()
            receive.close()
            if not completed:
                with anyio.move_on_after(1, shield=True):
                    await self.emit("media_error", state.id, code=code)

    def close(self) -> None:
        if self.transfer is not None:
            self.transfer.send.close()
