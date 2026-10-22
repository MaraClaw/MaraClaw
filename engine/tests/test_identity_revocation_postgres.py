import os
import sys
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import anyio
import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api import auth
from app.config import get_settings
from app.core.row_memo import clear_row_memo
from app.core.security import create_access_token, load_user_principal
from app.core.session_cache import get_cached_user, set_cached_user
from app.dao import identity_dao
from app.db import session as session_module
from app.db.connection import DbConnection
from app.db.pool import close_pool, init_pool
from app.db.session import bind_crud_connection, connection_ctx, transaction
from app.scripts.bootstrap_db import SCHEMA_BASELINE, _statement_bodies
from app.services import chat_tickets, file_download_tickets


@dataclass(frozen=True)
class RevocationDatabase:
    conninfo: str
    identity_id: UUID
    tenant_id: UUID
    users: tuple[UUID, UUID]
    agent_id: UUID

    async def generation(self) -> int:
        # A raw connection cannot inherit the request's connection ContextVar.
        async with await AsyncConnection[DictRow].connect(self.conninfo, row_factory=dict_row) as raw:
            row = await DbConnection(raw).fetchone(
                "SELECT auth_generation FROM identities WHERE id = %(id)s", {"id": self.identity_id}
            )
            assert row is not None
            return int(str(row["auth_generation"]))


@pytest.fixture
async def live_identity(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[RevocationDatabase]:
    conninfo = os.environ.get("BOOTSTRAP_TEST_DATABASE_URL", "")
    if not conninfo:
        pytest.skip("BOOTSTRAP_TEST_DATABASE_URL is required for isolated PostgreSQL QA")
    schema = f"identity_revocation_qa_{uuid4().hex}"
    live = RevocationDatabase(
        make_conninfo(conninfo, options=f"-csearch_path={schema}"), uuid4(), uuid4(), (uuid4(), uuid4()), uuid4()
    )
    for setting in ("USER_SESSION_CACHE_TTL_SECONDS", "AGENT_ACCESS_CACHE_TTL_SECONDS", "TENANT_CACHE_TTL_SECONDS"):
        monkeypatch.setattr(get_settings(), setting, 0)
    clear_row_memo()
    async with await AsyncConnection[DictRow].connect(conninfo, autocommit=True, row_factory=dict_row) as raw:
        db = DbConnection(raw)
        await db.execute(f'CREATE SCHEMA "{schema}"')
        context_token = session_module._conn_ctx.set(None)
        try:
            await db.execute(f'SET search_path TO "{schema}"')
            for statement in _statement_bodies(SCHEMA_BASELINE.read_text()):
                await db.execute(statement)
            await db.execute("INSERT INTO identities (id) VALUES (%(id)s)", {"id": live.identity_id})
            await db.execute(
                "INSERT INTO tenants (id, name, slug) VALUES (%(id)s, 'Revocation QA', %(slug)s)",
                {"id": live.tenant_id, "slug": uuid4().hex},
            )
            # The baseline permits one tenant-bound and additional tenantless memberships.
            for user_id, tenant_id in zip(live.users, (live.tenant_id, None), strict=True):
                await db.execute(
                    "INSERT INTO users (id, identity_id, tenant_id, display_name) "
                    "VALUES (%(id)s, %(identity)s, %(tenant)s, 'Revocation QA')",
                    {"id": user_id, "identity": live.identity_id, "tenant": tenant_id},
                )
            await db.execute(
                "INSERT INTO agents (id, name, role_description, creator_id, tenant_id, max_triggers, "
                "min_poll_interval_min, webhook_rate_limit, access_mode, company_access_level, "
                "heartbeat_interval_minutes) VALUES (%(id)s, 'QA', 'QA', %(owner)s, %(tenant)s, "
                "10, 5, 10, 'private', 'none', 30)",
                {"id": live.agent_id, "owner": live.users[0], "tenant": live.tenant_id},
            )
            await init_pool(conninfo=live.conninfo, min_size=1, max_size=4)
            yield live
        finally:
            try:
                await close_pool()
            finally:
                await db.execute(f'DROP SCHEMA "{schema}" CASCADE')
                clear_row_memo()
                session_module._conn_ctx.reset(context_token)
                _ = sys.stdout.write(f"IDENTITY_REVOCATION_SCHEMA_DROPPED={schema}\n")


@pytest.fixture
def auth_app() -> FastAPI:
    app = FastAPI()
    app.include_router(auth.router, prefix="/api", dependencies=[Depends(bind_crud_connection)])
    return app


class CommitObserver:
    def __init__(self, app: ASGIApp, live: RevocationDatabase) -> None:
        self.app = app
        self.live = live
        self.visible: list[int] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def observe(message: Message) -> None:
            if message["type"] == "http.response.start" and message["status"] == 204:
                self.visible.append(await self.live.generation())
            await send(message)

        await self.app(scope, receive, observe)


async def test_concurrent_bumps_return_distinct_consecutive_generations(live_identity: RevocationDatabase) -> None:
    # Given four genuinely separate PostgreSQL sessions released together.
    live = live_identity
    ready = anyio.Event()
    pids: list[int] = []
    generations: list[int | None] = []

    async def bump() -> None:
        async with await AsyncConnection[DictRow].connect(live.conninfo, row_factory=dict_row) as raw:
            pids.append(raw.info.backend_pid)
            if len(pids) == 4:
                ready.set()
            await ready.wait()
            async with transaction(DbConnection(raw)):
                generations.append(await identity_dao.bump_auth_generation(live.identity_id))

    # When the real DAO increments the same identity concurrently.
    with anyio.fail_after(15):
        async with anyio.create_task_group() as workers:
            for _ in range(4):
                workers.start_soon(bump)
    # Then UPDATE RETURNING loses no increment and each caller receives its own generation.
    assert len(set(pids)) == 4
    assert set(generations) == {1, 2, 3, 4}
    assert await live.generation() == 4


async def test_bump_joins_caller_transaction_and_rolls_back(live_identity: RevocationDatabase) -> None:
    # Given a fresh identity and an owning application transaction.
    assert await identity_dao.get_auth_generation(live_identity.identity_id) == 0

    async def abort_revocation() -> None:
        async with connection_ctx():
            assert await identity_dao.bump_auth_generation(live_identity.identity_id) == 1
            assert await live_identity.generation() == 0
            raise RuntimeError("abort revocation")

    # When a later operation aborts after the real DAO bump.
    with pytest.raises(RuntimeError, match="abort revocation"):
        await abort_revocation()
    # Then an independent PostgreSQL session still sees the original committed generation.
    assert await live_identity.generation() == 0


async def test_logout_commits_before_204_response_start(live_identity: RevocationDatabase, auth_app: FastAPI) -> None:
    # Given the real auth route, request transaction, bearer authentication and SQL observer.
    token = await create_access_token(str(live_identity.users[0]), "member", identity_id=live_identity.identity_id)
    observed = CommitObserver(auth_app, live_identity)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=observed), base_url="http://qa") as client:
        # When revoking the identity over HTTP.
        response = await client.post("/api/auth/logout-all", headers={"Authorization": f"Bearer {token}"})
    # Then another connection sees the bump before even the 204 headers are sent.
    assert response.status_code == 204
    assert response.content == b""
    assert observed.visible == [1]


async def test_revocation_rejects_both_cached_memberships_but_accepts_fresh_token(
    live_identity: RevocationDatabase, auth_app: FastAPI
) -> None:
    # Given two real memberships and their previously admitted generation-zero credentials.
    live = live_identity
    principals = [await load_user_principal(uid, fresh=True) for uid in live.users]
    tokens = [await create_access_token(str(uid), "member", identity_id=live.identity_id) for uid in live.users]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=auth_app), base_url="http://qa") as client:
        for token in tokens:
            assert (await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})).status_code == 200
        # When SQL revokes the identity while another worker still holds stale principal snapshots.
        assert await identity_dao.bump_auth_generation(live.identity_id) == 1
        for principal, token in zip(principals, tokens, strict=True):
            await set_cached_user(principal)
            assert await get_cached_user(principal.id) is principal
            response = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
            # Then stale cached identity data cannot admit either old credential.
            assert response.status_code == 401
        fresh = await create_access_token(str(live.users[1]), "member", identity_id=live.identity_id)
        assert (await client.get("/api/auth/me", headers={"Authorization": f"Bearer {fresh}"})).status_code == 200


@pytest.mark.parametrize("transport", ["chat", "download"])
async def test_old_ticket_generation_is_rejected_by_real_sql(live_identity: RevocationDatabase, transport: str) -> None:
    # Given a usable ticket binding created from an original generation-zero JWT.
    live = live_identity
    common = {
        "user_id": live.users[0],
        "tenant_id": live.tenant_id,
        "agent_id": live.agent_id,
        "auth_deadline": int(time.time()) + 3600,
        "auth_generation": 0,
    }
    chat = chat_tickets.ChatTicketBinding.model_validate({**common, "session_id": uuid4()})
    download = file_download_tickets.DownloadTicketBinding.model_validate(
        {**common, "path": "report.txt", "inline": False}
    )

    async def authorize() -> None:
        match transport:
            case "chat":
                await chat_tickets.authorize(chat)
            case "download":
                await file_download_tickets.authorize_binding(
                    download, agent_id=live.agent_id, path="report.txt", inline=False
                )
            case _:
                raise AssertionError(transport)

    await authorize()
    # When SQL advances the identity generation after ticket creation.
    assert await identity_dao.bump_auth_generation(live.identity_id) == 1
    # Then redemption authorization checks the original generation, not a freshly substituted one.
    with pytest.raises(HTTPException) as rejected:
        await authorize()
    assert rejected.value.status_code == 401
