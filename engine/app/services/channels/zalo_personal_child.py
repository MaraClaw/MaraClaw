import json
import os
import secrets
import subprocess
from collections.abc import Sequence
from contextlib import suppress
from datetime import UTC, datetime
from typing import final
from uuid import uuid4

import anyio
from anyio.abc import Process
from cryptography.exceptions import InvalidTag
from fastapi import HTTPException
from pydantic import JsonValue, ValidationError

from app.core.logging import logger
from app.dao.zalo_personal_auth_dao import read_auth
from app.dao.zalo_personal_dao import fail, fenced, reaped, renew
from app.db.errors import DbError, UniqueViolationError
from app.db.session import connection_ctx
from app.records.zalo_personal import ZaloConnection
from app.services.channels.zalo_personal_events import handle_frame
from app.services.channels.zalo_personal_journal import authorize_dispatch
from app.services.channels.zalo_personal_protocol import Closed, CredsSet, Message, child_frame

FRAME_LIMIT = 1048576


async def spawn_child(command: Sequence[str]) -> Process:
    path = os.environ.get("PATH")
    return await anyio.open_process(command, stderr=subprocess.DEVNULL, env={} if path is None else {"PATH": path})


@final
class ZaloChild:
    def __init__(self, connection: ZaloConnection) -> None:
        self.connection = connection
        self.token = secrets.token_urlsafe(32)
        self.process: Process | None = None
        self.write_lock = anyio.Lock()

    async def write(self, value: dict[str, JsonValue]) -> None:
        process = self.process
        if process is None or process.stdin is None:
            raise BrokenPipeError
        encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()
        if len(encoded) > FRAME_LIMIT:
            raise ValueError("zalo_frame_limit")
        async with self.write_lock:
            await process.stdin.send(encoded + b"\n")

    async def read(self) -> None:
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
                if isinstance(frame, CredsSet):
                    try:
                        await handle_frame(self.connection, frame)
                    except DbError, HTTPException, InvalidTag, ValueError:
                        await self.write(
                            {
                                "id": uuid4().hex,
                                "token": self.token,
                                "type": "result",
                                "request_id": frame.id,
                                "ok": False,
                            }
                        )
                        await fail(self.connection, "auth_store_failed")
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
                if isinstance(frame, Closed):
                    return
            if len(pending) > FRAME_LIMIT:
                raise ValueError("zalo_frame_limit")

    async def tick(self) -> None:
        while True:
            async with connection_ctx() as db:
                current = await fenced(self.connection)
                if current.expires_at is not None and current.expires_at <= datetime.now(UTC):
                    await fail(self.connection, "qr_expired")
                    return
                if not await renew(self.connection):
                    return
                await db.execute(
                    "UPDATE zalo_personal_sends SET state='uncertain',finished_at=now() "
                    "WHERE config_id=%(id)s AND claim_token=%(claim)s AND state='dispatch_started' "
                    "AND dispatched_at<now()-interval '25 seconds'",
                    {"id": current.config_id, "claim": current.claim_token},
                )
            await self.write({"type": "lease", "id": uuid4().hex, "token": self.token, "lease_ms": 30000})
            if current.state == "connected":
                command = await authorize_dispatch(self.connection)
                if command is not None:
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
            await anyio.sleep(1)

    async def run(self, command: Sequence[str]) -> None:
        try:
            credentials = await read_auth(self.connection)
            with anyio.CancelScope(shield=True):
                self.process = await spawn_child(command)
            await self.write(
                {"type": "init", "id": uuid4().hex, "token": self.token, "lease_ms": 30000, "credentials": credentials}
            )
            async with anyio.create_task_group() as tasks:

                async def read_owned() -> None:
                    await self.read()
                    tasks.cancel_scope.cancel()

                async def tick_owned() -> None:
                    await self.tick()
                    tasks.cancel_scope.cancel()

                tasks.start_soon(read_owned)
                tasks.start_soon(tick_owned)
        except* UniqueViolationError:
            await fail(self.connection, "account_in_use")
        except* DbError, InvalidTag:
            await fail(self.connection, "auth_store_failed")
        except* HTTPException:
            logger.debug("[ZaloPersonal] source_revoked config_id={}", self.connection.config_id)
        except* ValueError, ValidationError:
            await fail(self.connection, "protocol_error")
        except* OSError, anyio.BrokenResourceError, anyio.ClosedResourceError:
            await fail(self.connection, "transport_failed")
        finally:
            with anyio.CancelScope(shield=True):
                await self.stop()

    async def stop(self) -> None:
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
