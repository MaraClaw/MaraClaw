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
from app.core.permissions import is_agent_expired
from app.dao.whatsapp_personal_auth_dao import read_auth
from app.dao.whatsapp_personal_dao import get_personal, reaped, renew, revoke
from app.db.errors import DbError, UniqueViolationError
from app.db.session import connection_ctx
from app.records.whatsapp_personal import PersonalConnection
from app.services.channels.whatsapp_personal_access import personal_owner
from app.services.channels.whatsapp_personal_diagnostics import (
    Diagnostic,
    diagnostic_frame,
    failed,
    lifecycle,
    log_diagnostic,
    observed_frame,
)
from app.services.channels.whatsapp_personal_events import IdentityAmbiguousError
from app.services.channels.whatsapp_personal_journal import authorize_dispatch
from app.services.channels.whatsapp_personal_protocol import AuthGet, AuthSet, Closed, CredsSet


class AuthStoreFailedError(Exception):
    pass


async def spawn_child(command: Sequence[str]) -> Process:
    path = os.environ.get("PATH")
    return await anyio.open_process(command, stderr=subprocess.DEVNULL, env={} if path is None else {"PATH": path})


@final
class PersonalChild:
    def __init__(self, connection: PersonalConnection) -> None:
        self.connection = connection
        self.token = secrets.token_urlsafe(32)
        self.run_id = uuid4()
        self.process: Process | None = None
        self.write_lock = anyio.Lock()

    async def write(self, value: dict[str, JsonValue]) -> None:
        process = self.process
        if process is None or process.stdin is None:
            raise BrokenPipeError
        async with self.write_lock:
            await process.stdin.send((json.dumps(value, separators=(",", ":")) + "\n").encode())

    async def read(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            raise BrokenPipeError
        pending = b""
        while True:
            try:
                pending += await process.stdout.receive(65536)
            except anyio.EndOfStream:
                lifecycle(self.connection, self.run_id, "parent_eof", "finished")
                return
            while b"\n" in pending:
                raw, pending = pending.split(b"\n", 1)
                if len(raw) > 1048576:
                    raise ValueError("personal_frame_limit")
                frame = diagnostic_frame.validate_json(raw)
                if not secrets.compare_digest(frame.token.encode(), self.token.encode()):
                    raise ValueError("personal_frame_token")
                if isinstance(frame, Diagnostic):
                    log_diagnostic(self.connection, self.run_id, frame)
                    continue
                try:
                    result = await observed_frame(self.connection, self.run_id, frame)
                except HTTPException as error:
                    if isinstance(frame, (AuthGet, AuthSet, CredsSet)):
                        await self.write(
                            {
                                "type": "result",
                                "id": frame.id,
                                "token": self.token,
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
                        await self.write(
                            {
                                "type": "result",
                                "id": frame.id,
                                "token": self.token,
                                "ok": True,
                                "value": result,
                                "error": None,
                            }
                        )
                if isinstance(frame, Closed):
                    return
            if len(pending) > 1048576:
                raise ValueError("personal_frame_limit")

    async def tick(self) -> None:
        while True:
            async with connection_ctx() as db:
                agent, _ = await personal_owner(self.connection.agent_id, authorization_only=True)
                current = await get_personal(self.connection.agent_id)
                stopping = is_agent_expired(agent) or agent.status == "stopped"
                expiring = (
                    current is not None and current.expires_at is not None and current.expires_at <= datetime.now(UTC)
                )
                if not stopping:
                    if current is None or not await renew(self.connection):
                        return
                    if not expiring:
                        await db.execute(
                            "UPDATE whatsapp_personal_sends SET state='uncertain',finished_at=now() "
                            "WHERE config_id=%(id)s AND claim_token=%(claim)s AND state='dispatch_started' "
                            "AND dispatched_at<now()-interval '25 seconds'",
                            {"id": current.config_id, "claim": current.claim_token},
                        )
            if stopping or expiring:
                # Never upgrade shared owner locks: revalidate after a fresh exclusive acquisition.
                async with connection_ctx():
                    agent, _ = await personal_owner(self.connection.agent_id)
                    current = await get_personal(self.connection.agent_id, lock=True)
                    if is_agent_expired(agent) or agent.status == "stopped":
                        await revoke(agent.id, purge=False)
                        return
                    if (
                        current is not None
                        and current.expires_at is not None
                        and current.expires_at <= datetime.now(UTC)
                    ):
                        if not await renew(self.connection):
                            return
                        await revoke(current.agent_id, purge=True)
                        return
                continue
            await self.write({"type": "lease", "id": secrets.token_hex(8), "token": self.token, "lease_ms": 30000})
            command = await authorize_dispatch(self.connection)
            if command is not None:
                await self.write(
                    {
                        "type": "send",
                        "id": str(command.command_id),
                        "token": self.token,
                        "command_id": str(command.command_id),
                        "message_id": command.message_id,
                        "self_jid": command.self_jid,
                        "text": command.text,
                        "timeout_ms": 20000,
                    }
                )
            await anyio.sleep(1)

    async def run(self, command: Sequence[str]) -> None:
        try:
            lifecycle(self.connection, self.run_id, "parent_initial_read", "start")
            creds = await read_auth(self.connection, "creds", ["main"])
            lifecycle(self.connection, self.run_id, "parent_start", "start")
            with anyio.CancelScope(shield=True):
                self.process = await spawn_child(command)
            await self.write(
                {"type": "init", "id": "init", "token": self.token, "creds": creds.get("main"), "lease_ms": 30000}
            )
            async with anyio.create_task_group() as tasks:

                async def owned_read() -> None:
                    await self.read()
                    tasks.cancel_scope.cancel()

                async def owned_tick() -> None:
                    await self.tick()
                    tasks.cancel_scope.cancel()

                tasks.start_soon(owned_read)
                tasks.start_soon(owned_tick)
        except Exception as error:
            failed(self.connection, self.run_id, error)
            raise
        finally:
            with anyio.CancelScope(shield=True):
                await self.stop()

    async def stop(self) -> None:
        process = self.process
        if process is not None:
            try:
                with anyio.move_on_after(3):
                    await self.write({"type": "shutdown", "id": "shutdown", "token": self.token})
                    await process.wait()
            except BrokenPipeError, anyio.BrokenResourceError, anyio.ClosedResourceError:
                pass
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
            await process.aclose()
            lifecycle(self.connection, self.run_id, "parent_exit", "finished", process.returncode)
        await reaped(self.connection)


async def run_child(connection: PersonalConnection, command: Sequence[str]) -> None:
    try:
        child = PersonalChild(connection)
        await child.run(command)
        if child.process is not None and child.process.returncode != 0:
            await fail_connection(connection, "transport_failed")
    except* UniqueViolationError:
        await fail_connection(connection, "account_in_use")
    except* IdentityAmbiguousError:
        await fail_connection(connection, "identity_ambiguous")
    except* DbError, InvalidTag, AuthStoreFailedError:
        await fail_connection(connection, "auth_store_failed")
    except* HTTPException:
        await fail_connection(connection, "transport_failed")
    except* OSError, ValueError, ValidationError, anyio.BrokenResourceError:
        logger.warning("[WhatsAppPersonal] child_failed")
        await fail_connection(connection, "transport_failed")


async def fail_connection(connection: PersonalConnection, code: str) -> None:
    async with connection_ctx() as db:
        try:
            await personal_owner(connection.agent_id)
        except HTTPException:
            # Membership loss still requires purging; lifecycle locks acquired before principal validation remain held.
            logger.debug("[WhatsAppPersonal] owner_unavailable")
        current = await get_personal(connection.agent_id, lock=True)
        if (
            current is None
            or current.generation != connection.generation
            or current.auth_epoch != connection.auth_epoch
            or current.claim_token is not None
        ):
            return
        await revoke(connection.agent_id, purge=True)
        await db.execute(
            "UPDATE whatsapp_personal_connections SET state='failed',error_code=%(code)s,revision=revision+1 "
            "WHERE config_id=%(id)s AND claim_token IS NULL",
            {"id": connection.config_id, "code": code},
        )
