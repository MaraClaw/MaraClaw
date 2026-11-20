import secrets
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

import anyio
from anyio.abc import Process
from anyio.streams.memory import MemoryObjectSendStream
from fastapi import HTTPException
from pydantic import JsonValue

from app.core.logging import logger
from app.records.whatsapp_personal import PersonalConnection
from app.services.channels.whatsapp_personal_diagnostics import (
    Diagnostic,
    diagnostic_frame,
    lifecycle,
    log_diagnostic,
    observed_frame,
)
from app.services.channels.whatsapp_personal_media_transport import MediaTransferError, MediaTransport
from app.services.channels.whatsapp_personal_protocol import (
    AuthGet,
    AuthSet,
    Closed,
    CredsSet,
    Identity,
    MediaChunk,
    MediaEnd,
    MediaError,
    Message,
)


class AuthStoreFailedError(Exception):
    pass


class ParentPeer(Protocol):
    connection: PersonalConnection
    run_id: UUID
    token: str
    process: Process | None
    media: MediaTransport

    async def write(self, value: dict[str, JsonValue]) -> None: ...

    def observe_link(self, *, up: bool) -> None: ...


@dataclass(frozen=True, slots=True)
class IntakeQueues:
    text: MemoryObjectSendStream[Message]
    media: MemoryObjectSendStream[Message]
    text_drained: anyio.Event


async def read_frames(peer: ParentPeer, messages: IntakeQueues) -> Closed | None:
    """Consume child frames until EOF or the handled ``closed`` frame, which is returned."""
    process = peer.process
    if process is None or process.stdout is None:
        raise BrokenPipeError
    pending = b""
    while True:
        try:
            pending += await process.stdout.receive(65536)
        except anyio.EndOfStream:
            lifecycle(peer.connection, peer.run_id, "parent_eof", "finished")
            messages.text.close()
            await messages.text_drained.wait()
            return None
        while b"\n" in pending:
            raw, pending = pending.split(b"\n", 1)
            if len(raw) > 1048576:
                raise MediaTransferError("too_large")
            frame = diagnostic_frame.validate_json(raw)
            if not secrets.compare_digest(frame.token.encode(), peer.token.encode()):
                raise MediaTransferError("forbidden")
            if isinstance(frame, Diagnostic):
                log_diagnostic(peer.connection, peer.run_id, frame)
                if frame.stage == "connection" and frame.connection_state == "close":
                    peer.observe_link(up=False)
                continue
            if isinstance(frame, (MediaChunk, MediaEnd, MediaError)):
                await peer.media.handle(frame)
                continue
            if isinstance(frame, Message):
                if frame.media is None:
                    await messages.text.send(frame)
                else:
                    try:
                        messages.media.send_nowait(frame)
                    except anyio.WouldBlock:
                        logger.bind(config_id=str(peer.connection.config_id), run_id=str(peer.run_id)).warning(
                            "[WhatsAppPersonal] media_queue_full"
                        )
                continue
            if isinstance(frame, Closed):
                messages.text.close()
                await messages.text_drained.wait()
            try:
                result = await observed_frame(peer.connection, peer.run_id, frame)
            except HTTPException as error:
                if isinstance(frame, (AuthGet, AuthSet, CredsSet)):
                    await peer.write(
                        {
                            "type": "result",
                            "id": frame.id,
                            "token": peer.token,
                            "ok": False,
                            "value": None,
                            "error": "auth_store_failed",
                        }
                    )
                if error.status_code not in {403, 404, 409, 429}:
                    raise
                if isinstance(frame, (AuthGet, AuthSet, CredsSet)):
                    raise AuthStoreFailedError from None
            else:
                if isinstance(frame, (AuthGet, AuthSet, CredsSet)):
                    await peer.write(
                        {
                            "type": "result",
                            "id": frame.id,
                            "token": peer.token,
                            "ok": True,
                            "value": result,
                            "error": None,
                        }
                    )
                elif isinstance(frame, Identity):
                    peer.observe_link(up=True)
            if isinstance(frame, Closed):
                return frame
        if len(pending) > 1048576:
            raise MediaTransferError("too_large")
