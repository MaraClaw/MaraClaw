import os
from hashlib import sha256
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from psycopg.conninfo import make_conninfo

from app.api import linkup_proxy
from app.db.connection import DbConnection
from app.db.pool import close_pool, init_pool
from app.db.session import connection_ctx
from app.services.agent_manager import AgentManager
from app.services.linkup.tokens import _proxy_token, make_proxy_token
from test_openclaw_update_postgres import agent, db, member, tenant

__all__ = ["db"]


async def request(token: str, monkeypatch: pytest.MonkeyPatch) -> tuple[int, AsyncMock]:
    upstream = AsyncMock(return_value=(200, "{}", {}))
    monkeypatch.setattr(linkup_proxy, "proxy_linkup", upstream)
    app = FastAPI()
    app.include_router(linkup_proxy.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://proxy.test") as client:
        response = await client.post("/api/linkup/v1/search", headers={"Authorization": f"Bearer {token}"})
    return response.status_code, upstream


async def new_agent(db: DbConnection) -> UUID:
    tenant_id = await tenant(db)
    return await agent(db, tenant_id, await member(db, tenant_id))


async def test_unissued_signed_token_is_rejected(db: DbConnection, monkeypatch: pytest.MonkeyPatch) -> None:
    agent_id = await new_agent(db)
    status, upstream = await request(_proxy_token(agent_id), monkeypatch)
    assert status == 401
    upstream.assert_not_awaited()


async def test_issuance_persists_hash(db: DbConnection) -> None:
    agent_id = await new_agent(db)
    token = await make_proxy_token(agent_id)
    stored = await db.fetchval("SELECT token_hash FROM linkup_proxy_tokens WHERE agent_id=%(id)s", {"id": agent_id})
    assert stored == sha256(token.encode()).digest()
    assert await make_proxy_token(agent_id) == token
    assert await db.fetchval("SELECT count(*) FROM linkup_proxy_tokens WHERE agent_id=%(id)s", {"id": agent_id}) == 1


@pytest.mark.parametrize("state", ["issued", "foreign", "revoked", "expired_flag", "elapsed"])
async def test_proxy_requires_matching_live_binding(
    db: DbConnection, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    agent_id = await new_agent(db)
    token = await make_proxy_token(agent_id)
    if state == "foreign":
        other_token = await make_proxy_token(await new_agent(db))
        await db.execute(
            "UPDATE linkup_proxy_tokens SET token_hash=%(hash)s WHERE agent_id=%(id)s",
            {"hash": sha256(other_token.encode()).digest(), "id": agent_id},
        )
    elif state == "revoked":
        await db.execute("DELETE FROM linkup_proxy_tokens WHERE agent_id=%(id)s", {"id": agent_id})
    elif state == "expired_flag":
        await db.execute("UPDATE agents SET is_expired=true WHERE id=%(id)s", {"id": agent_id})
    elif state == "elapsed":
        await db.execute("UPDATE agents SET expires_at=now()-interval '1 second' WHERE id=%(id)s", {"id": agent_id})
    status, upstream = await request(token, monkeypatch)
    assert status == (200 if state == "issued" else 401)
    assert upstream.await_count == (1 if state == "issued" else 0)


async def test_deleting_agent_cascades_token(db: DbConnection) -> None:
    agent_id = await new_agent(db)
    await make_proxy_token(agent_id)
    await db.execute("DELETE FROM agents WHERE id=%(id)s", {"id": agent_id})
    assert await db.fetchval("SELECT count(*) FROM linkup_proxy_tokens WHERE agent_id=%(id)s", {"id": agent_id}) == 0


async def test_start_issuance_survives_request_rollback(db: DbConnection, monkeypatch: pytest.MonkeyPatch) -> None:
    agent_id = await new_agent(db)
    manager = AgentManager.__new__(AgentManager)
    monkeypatch.setattr("app.services.agent_manager.settings.LINKUP_PROXY_ENABLED", True)

    async def request_that_rolls_back() -> None:
        async with connection_ctx():
            await manager._prepare_linkup_proxy_token(agent_id)
            raise RuntimeError("rollback")

    with pytest.raises(RuntimeError, match="rollback"):
        await request_that_rolls_back()
    schema = await db.fetchval("SELECT current_schema()")
    await close_pool()
    await init_pool(
        conninfo=make_conninfo(os.environ["BOOTSTRAP_TEST_DATABASE_URL"], options=f"-csearch_path={schema}")
    )
    status, upstream = await request(_proxy_token(agent_id), monkeypatch)
    assert status == 200
    upstream.assert_awaited_once()
