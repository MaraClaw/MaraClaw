import os
import socket
import socketserver
import threading
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import assert_never, override
from uuid import UUID, uuid4

import pytest
from anyio import Path
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row
from redis.asyncio import Redis

from app.api import auth
from app.config import Settings
from app.core import events
from app.dao.identity_dao import identity_dao
from app.dao.tenant_dao import tenant_dao
from app.dao.tenant_email_dao import tenant_email_dao
from app.db.connection import DbConnection
from app.db.pool import close_pool, init_pool
from app.db.session import bind_crud_connection
from app.records.auth_email import AuthEmailKind, AuthEmailMaterial
from app.records.identity import IdentityRecord
from app.services import auth_email_delivery as delivery, frontend_origin, password_reset_service as reset
from app.services.email_verification_service import email_verification_service as verification


class MailSink(socketserver.ThreadingTCPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.messages: list[bytes] = []
        self.rcpt_reply: bytes = b"250 OK\r\n"
        self.lose_ack: bool = False
        self.received: threading.Event = threading.Event()
        self.release: threading.Event = threading.Event()
        self.release.set()


class _Handler(socketserver.StreamRequestHandler):
    connection: socket.socket

    @override
    def handle(self) -> None:
        sink = self.server
        if not isinstance(sink, MailSink):
            raise TypeError("authentication SMTP fixture requires MailSink")
        self.connection.settimeout(5)
        self.wfile.write(b"220 sink\r\n")
        while line := self.rfile.readline(65536):
            verb = line.split(b" ", 1)[0].strip().upper()
            if verb in {b"EHLO", b"HELO", b"MAIL", b"RSET", b"NOOP"}:
                self.wfile.write(b"250 OK\r\n")
            elif verb == b"RCPT":
                self.wfile.write(sink.rcpt_reply)
            elif verb == b"DATA":
                self.wfile.write(b"354 go\r\n")
                body = bytearray()
                while chunk := self.rfile.readline(65536):
                    if chunk == b".\r\n":
                        break
                    body.extend(chunk)
                sink.messages.append(bytes(body))
                sink.received.set()
                if not sink.release.wait(5):
                    return
                if sink.lose_ack:
                    return
                self.wfile.write(b"250 stored\r\n")
            elif verb == b"QUIT":
                self.wfile.write(b"221 bye\r\n")
                return
            else:
                self.wfile.write(b"502 unsupported\r\n")


@pytest.fixture
def sink() -> Iterator[MailSink]:
    server = MailSink()
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield server
    finally:
        server.release.set()
        server.shutdown()
        server.server_close()
        thread.join(5)
        assert not thread.is_alive()


@dataclass(frozen=True, slots=True)
class AuthEmailScenario:
    db: DbConnection
    client: AsyncClient
    redis: Redis
    identity: IdentityRecord
    platform_id: UUID

    async def prepare(self, kind: AuthEmailKind = AuthEmailKind.RESET) -> tuple[UUID, str, datetime]:
        identity = self.identity
        assert identity.email is not None
        match kind:
            case AuthEmailKind.VERIFICATION:
                credential, expiry = await verification.create_email_verification_token(identity.id, identity.email)
                url = await verification.build_email_verification_url("https://trusted.example", credential)
            case AuthEmailKind.RESET:
                credential, expiry = await reset.create_password_reset_token(identity.id)
                url = await reset.build_password_reset_url(credential)
            case unreachable:
                assert_never(unreachable)
        job_id = await delivery.accept(
            identity,
            AuthEmailMaterial(identity.email, "Member", credential, url, delivery.password_fingerprint(identity)),
            kind=kind,
            expires_at=expiry,
        )
        return job_id, credential, expiry

    async def state(self) -> dict[str, object]:
        row = await self.db.fetchone(
            "SELECT status,last_error,payload IS NULL AS payload_gone,attempts FROM auth_email_jobs ORDER BY created_at DESC LIMIT 1"
        )
        assert row is not None
        return row

    async def due(self) -> None:
        await self.db.execute("UPDATE auth_email_jobs SET available_at=now() - INTERVAL '1 second'")


@pytest.fixture
async def scenario(monkeypatch: pytest.MonkeyPatch, sink: MailSink) -> AsyncIterator[AuthEmailScenario]:
    conninfo = os.environ.get("BOOTSTRAP_TEST_DATABASE_URL", "")
    redis_url = os.environ.get("AUTH_EMAIL_TEST_REDIS_URL", "")
    if not conninfo or not redis_url:
        pytest.skip("BOOTSTRAP_TEST_DATABASE_URL and AUTH_EMAIL_TEST_REDIS_URL required for isolated auth mail QA")
    schema = f"auth_email_qa_{uuid4().hex}"
    settings = Settings(PUBLIC_BASE_URL="https://trusted.example", USER_SESSION_CACHE_TTL_SECONDS=0)
    monkeypatch.setattr(frontend_origin, "get_settings", lambda: settings)
    monkeypatch.setattr("app.services.platform_service.get_settings", lambda: settings)
    async with Redis.from_url(redis_url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2) as redis:
        monkeypatch.setattr(events, "_redis_client", redis)
        async with await AsyncConnection[DictRow].connect(conninfo, autocommit=True, row_factory=dict_row) as raw:
            db = DbConnection(raw)
            await db.execute(f'CREATE SCHEMA "{schema}"')
            try:
                await db.execute(f'SET search_path TO "{schema}"')
                await db.execute(await Path("scripts/schema_baseline.sql").read_text())
                await db.execute(await Path("scripts/schema_baseline.sql").read_text())
                await init_pool(
                    conninfo=make_conninfo(conninfo, options=f"-csearch_path={schema}"), min_size=1, max_size=6
                )
                identity = await identity_dao.create(
                    obj_in={"email": "member@example.com", "password_hash": "original-hash"}
                )
                platform = await tenant_dao.create(obj_in={"name": "MaraClaw", "slug": "maraclaw", "is_system": True})
                await tenant_email_dao.put(
                    platform.id,
                    {
                        "enabled": True,
                        "from_address": "noreply@example.com",
                        "smtp_host": "127.0.0.1",
                        "smtp_port": sink.server_address[1],
                        "smtp_username": "",
                        "password": "",
                        "smtp_ssl": False,
                        "smtp_timeout_seconds": 3,
                        "public_only": False,
                    },
                )
                app = FastAPI()
                app.include_router(auth.router, dependencies=[Depends(bind_crud_connection)])
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://auth.test") as client:
                    yield AuthEmailScenario(db, client, redis, identity, platform.id)
            finally:
                await close_pool()
                await db.execute(f'DROP SCHEMA "{schema}" CASCADE')
