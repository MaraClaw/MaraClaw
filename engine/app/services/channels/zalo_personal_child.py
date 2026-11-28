import json
import os
import secrets
import subprocess
from collections.abc import AsyncIterator, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from typing import final
from uuid import UUID, uuid4

import anyio
from anyio.abc import Process, TaskGroup
from cryptography.exceptions import InvalidTag
from fastapi import HTTPException
from pydantic import JsonValue, ValidationError

from app.config import get_settings
from app.core.logging import logger
from app.dao.zalo_personal_auth_dao import read_auth
from app.dao.zalo_personal_dao import fail, fenced, reaped, renew
from app.dao.zalo_personal_runtime_dao import has_auth, interrupted
from app.db.errors import DbError, UniqueViolationError
from app.db.session import connection_ctx
from app.records.channel_media import StoredFile
from app.records.zalo_personal import ZaloConnection
from app.services.channels.zalo_personal_dispatch_probe import has_pending, settle_uncertain
from app.services.channels.zalo_personal_events import handle_frame
from app.services.channels.zalo_personal_journal import authorize_dispatch
from app.services.channels.zalo_personal_media_jobs import consume_media, dispatch_file, send_pending_media
from app.services.channels.zalo_personal_media_journal import has_pending_media
from app.services.channels.zalo_personal_media_transport import MediaTransferError, MediaTransport
from app.services.channels.zalo_personal_protocol import (
    ChildFrame,
    Closed,
    CredsSet,
    MediaAck,
    MediaChunk,
    MediaEnd,
    MediaError,
    MediaReady,
    Message,
    SendResult,
    child_frame,
)
from app.services.channels.zalo_personal_typing import TypingPump

FRAME_LIMIT = 1048576


async def spawn_child(command: Sequence[str]) -> Process:
    path = os.environ.get("PATH")
    heap = f"--worker-heap-mb={get_settings().ZALO_PERSONAL_WORKER_HEAP_MB}"
    return await anyio.open_process(
        [*command, heap], stderr=subprocess.DEVNULL, env={} if path is None else {"PATH": path}
    )


@final
class ZaloChild:
    def __init__(self, connection: ZaloConnection) -> None:
        self.connection = connection
        self.token = secrets.token_urlsafe(32)
        self.process: Process | None = None
        self.write_lock = anyio.Lock()
        self.settled = False
        self.typing = TypingPump()
        self.media = MediaTransport(self.write, self.token)
        self.tasks: TaskGroup | None = None
        self.media_busy = False
        self.media_results: dict[str, anyio.Event] = {}
        self.dispatched: frozenset[UUID] = frozenset()

    async def write(self, value: dict[str, JsonValue]) -> None:
        process = self.process
        if process is None or process.stdin is None:
            raise BrokenPipeError
        encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()
        if len(encoded) > FRAME_LIMIT:
            raise ValueError("zalo_frame_limit")
        async with self.write_lock:
            await process.stdin.send(encoded + b"\n")

    async def frames(self) -> AsyncIterator[ChildFrame]:
        process = self.process
        if process is None or process.stdout is None:
            raise BrokenPipeError
        pending = b""
        while True:
            try:
                pending += await process.stdout.receive(65536)
            except anyio.EndOfStream:
                raise BrokenPipeError("zalo_child_eof") from None
            while b"\n" in pending:
                raw, pending = pending.split(b"\n", 1)
                if len(raw) > FRAME_LIMIT:
                    raise ValueError("zalo_frame_limit")
                frame = child_frame.validate_json(raw)
                if not secrets.compare_digest(frame.token.encode(), self.token.encode()):
                    raise ValueError("zalo_frame_token")
                yield frame
            if len(pending) > FRAME_LIMIT:
                raise ValueError("zalo_frame_limit")

    async def read(self) -> None:
        send, receive = anyio.create_memory_object_stream[Message](64)
        async with send, receive, anyio.create_task_group() as jobs:
            jobs.start_soon(consume_media, self, receive.clone())
            jobs.start_soon(consume_media, self, receive.clone())
            async for frame in self.frames():
                if isinstance(frame, (MediaAck, MediaChunk, MediaEnd, MediaError, MediaReady)):
                    await self.media.handle(frame)
                    continue
                if isinstance(frame, Message) and frame.media is not None:
                    try:
                        send.send_nowait(frame)
                    except anyio.WouldBlock:
                        raise BrokenPipeError("zalo_media_intake_full") from None
                    continue
                if isinstance(frame, CredsSet):
                    try:
                        await handle_frame(self.connection, frame)
                    except DbError, HTTPException, InvalidTag, ValueError:
                        await interrupted(self.connection, "auth_store_failed")
                        self.settled = True
                        await self.write(
                            {
                                "id": uuid4().hex,
                                "token": self.token,
                                "type": "result",
                                "request_id": frame.id,
                                "ok": False,
                            }
                        )
                        jobs.cancel_scope.cancel()
                        return
                    await self.write(
                        {"id": uuid4().hex, "token": self.token, "type": "result", "request_id": frame.id, "ok": True}
                    )
                else:
                    try:
                        await handle_frame(self.connection, frame)
                    except HTTPException as error:
                        if not isinstance(frame, Message) or error.status_code not in {403, 404, 409, 429, 507}:
                            raise
                if isinstance(frame, SendResult):
                    result = self.media_results.get(frame.command_id)
                    if result is not None:
                        result.set()
                if isinstance(frame, Closed):
                    self.settled = True
                    jobs.cancel_scope.cancel()
                    return

    async def download_message_media(self, message: Message) -> bytes:
        return await self.media.download(message)

    async def send_file(self, command_id: str, peer_id: str, file: StoredFile, data: bytes) -> None:
        await dispatch_file(self, command_id, peer_id, file, data)

    async def tick(self) -> None:
        while True:
            waiting = media_waiting = False
            async with connection_ctx():
                current = await fenced(self.connection, lock=False)
                if current.expires_at is not None and current.expires_at <= datetime.now(UTC):
                    if current.account_id is not None and await has_auth(current):
                        await interrupted(self.connection)
                    else:
                        await fail(self.connection, "qr_expired")
                    self.settled = True
                    return
                if not await renew(self.connection, current):
                    return
                if self.dispatched:
                    self.dispatched = await settle_uncertain(current, self.dispatched)
                if current.state == "connected":
                    waiting = await has_pending(current)
                    media_waiting = await has_pending_media(current)
            await self.write({"type": "lease", "id": uuid4().hex, "token": self.token, "lease_ms": 30000})
            if current.state == "connected":
                if media_waiting and self.tasks is not None and not self.media_busy:
                    self.media_busy = True
                    self.tasks.start_soon(send_pending_media, self)
                command = await authorize_dispatch(self.connection) if waiting else None
                if command is not None:
                    self.dispatched = self.dispatched | {command.command_id}
                    await self.write(
                        {
                            "type": "send",
                            "id": uuid4().hex,
                            "token": self.token,
                            "command_id": str(command.command_id),
                            "thread_id": command.peer_id,
                            "text": command.text,
                        }
                    )
                for peer in await self.typing.due(current):
                    await self.write({"type": "typing", "id": uuid4().hex, "token": self.token, "thread_id": peer})
            await anyio.sleep(1)

    async def run(self, command: Sequence[str]) -> None:
        try:
            credentials = await read_auth(self.connection)
            with anyio.CancelScope(shield=True):
                self.process = await spawn_child(command)
            await self.write(
                {
                    "type": "init",
                    "id": uuid4().hex,
                    "token": self.token,
                    "lease_ms": 30000,
                    "credentials": credentials,
                    "keepalive_interval_ms": get_settings().ZALO_PERSONAL_KEEPALIVE_INTERVAL_SECONDS * 1000,
                    "media_allowed_hosts": list(get_settings().ZALO_PERSONAL_MEDIA_ALLOWED_HOSTS),
                    "media_transfer_timeout_ms": 120000,
                }
            )
            async with anyio.create_task_group() as tasks:
                self.tasks = tasks

                async def read_owned() -> None:
                    await self.read()
                    tasks.cancel_scope.cancel()

                async def tick_owned() -> None:
                    await self.tick()
                    tasks.cancel_scope.cancel()

                tasks.start_soon(read_owned)
                tasks.start_soon(tick_owned)
        except* UniqueViolationError:
            if not self.settled:
                await fail(self.connection, "account_in_use")
        except* DbError, InvalidTag:
            if not self.settled:
                await interrupted(self.connection, "auth_store_failed")
        except* HTTPException:
            logger.debug("[ZaloPersonal] source_revoked config_id={}", self.connection.config_id)
        except* ValueError, ValidationError:
            if not self.settled:
                await fail(self.connection, "protocol_error")
        except* OSError, anyio.BrokenResourceError, anyio.ClosedResourceError, MediaTransferError:
            if not self.settled:
                await interrupted(self.connection)
        finally:
            with anyio.CancelScope(shield=True):
                await self.stop()

    async def stop(self) -> None:
        self.media.close()
        process = self.process
        if process is not None:
            try:
                with anyio.move_on_after(3):
                    await self.write({"type": "shutdown", "id": uuid4().hex, "token": self.token})
                    await process.wait()
            except BrokenPipeError, anyio.BrokenResourceError, anyio.ClosedResourceError:
                pass
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
            await process.aclose()
        await reaped(self.connection)


async def run_child(connection: ZaloConnection, command: Sequence[str]) -> None:
    await ZaloChild(connection).run(command)
