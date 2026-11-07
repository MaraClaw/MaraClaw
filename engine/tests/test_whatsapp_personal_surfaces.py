import asyncio
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from time import monotonic
from uuid import uuid4

import anyio
import httpx
import pytest
from fastapi import FastAPI
from tests.channel_submission_fixtures import channel_db, receipt_db
from tests.test_whatsapp_personal_integration import connected

from app.api.whatsapp import router
from app.core.security import get_current_user
from app.dao.agent_dao import agent_dao
from app.dao.whatsapp_personal_dao import claim, get_personal
from app.db import session as db_session
from app.db.pool import close_pool, init_pool
from app.db.session import transaction
from app.records.whatsapp_personal import PersonalConnection
from app.services.channels import whatsapp_personal_supervisor as supervisor
from app.services.channels.whatsapp_personal_child import PersonalChild
from app.services.channels.whatsapp_personal_control import start

__all__ = ["channel_db", "receipt_db"]


def use_pool(f, monkeypatch):
    class Pool:
        @asynccontextmanager
        async def connection(self):
            yield f.db.first.raw

    monkeypatch.setattr(db_session, "get_pool", lambda: Pool())


@asynccontextmanager
async def personal_pool(f):
    await init_pool(conninfo=f.db.conninfo, min_size=1, max_size=4)
    try:
        yield
    finally:
        await close_pool()


async def test_public_personal_api_runs_fresh_generation_contract(channel_db, monkeypatch):
    # Given the real router backed by isolated PostgreSQL.
    f = channel_db
    use_pool(f, monkeypatch)
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: f.db.user
    path = f"/api/agents/{f.config.agent_id}/whatsapp-channel"
    # When a creator starts and unlinks through HTTP.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://qa") as client:
        initial = await client.get(path + "/personal")
        response = await client.post(path + "/personal/start", json={"expected_generation": None})
        configured = await client.get(path)
        generation = response.json()["generation"]
        stale = await client.post(path + "/personal/start", json={"expected_generation": None})
        removed = await client.delete(path + "/personal", params={"expected_generation": generation})
    # Then status, mode, stale rejection and privacy headers match the frozen API.
    assert initial.status_code == 200
    assert initial.json()["generation"] is None
    assert response.status_code == 202
    assert response.json()["state"] == "pending"
    assert response.headers["cache-control"] == "no-store, private"
    assert configured.status_code == 200
    assert configured.json()["mode"] == "personal_qr"
    assert configured.json()["app_secret"] is None
    assert stale.status_code == 409
    assert removed.status_code == 200
    assert removed.json()["state"] == "disconnected"


async def test_supervisor_private_pipe_auth_ack_and_exact_reap(channel_db):
    # Given a real child process speaking only the frozen JSONL protocol.
    f = channel_db
    async with transaction(f.db.first):
        await start(f.config.agent_id, f.db.user.id, None)
        owner = await claim(f.config.id)
        assert owner is not None
    script = """
import json,sys
init=json.loads(sys.stdin.readline())
token=init['token']
def emit(value):
 value.update(token=token)
 print(json.dumps(value),flush=True)
def result(ident):
 while True:
  frame=json.loads(sys.stdin.readline())
  if frame['type']=='result' and frame['id']==ident:
   assert frame['ok'] is True
   return frame['value']
emit({'type':'auth_set','id':'set','batches':[{'category':'arbitrary-key','values':{'one':{'type':'Buffer','data':[1,2]}}}]})
result('set')
emit({'type':'auth_get','id':'get','category':'arbitrary-key','ids':['one','missing']})
assert result('get')=={'one':{'type':'Buffer','data':[1,2]}}
emit({'type':'creds_set','id':'creds','creds':'{"registered":true}'})
result('creds')
emit({'type':'identity','id':'identity','account_id':'15550001@s.whatsapp.net','pn_jid':'15550001@s.whatsapp.net','lid_jid':None})
emit({'type':'closed','id':'closed','reason':'shutdown'})
"""
    child = PersonalChild(owner)
    # When auth mutations are acknowledged over inherited pipes.
    async with personal_pool(f):
        with anyio.fail_after(10):
            await child.run((sys.executable, "-u", "-c", script))
    # Then ACKs reflect committed encrypted storage and the exact child exited.
    assert child.process is not None
    assert child.process.returncode == 0
    async with transaction(f.db.second):
        row = await get_personal(owner.agent_id)
        assert row is not None
        assert row.claim_token is None
        assert row.account_id == "15550001@s.whatsapp.net"
        assert await f.db.second.fetchval("SELECT count(*) FROM whatsapp_personal_auth") == 2


async def test_lifecycle_stop_revokes_route_and_owner_change_purges(channel_db):
    # Given encrypted personal auth on a live claimed account.
    f = channel_db
    async with transaction(f.db.first):
        owner = await connected(f)
        agent = await agent_dao.get(owner.agent_id)
        assert agent is not None
    # When the existing state owner stops the agent.
    async with transaction(f.db.first):
        stopped = await agent_dao.update(db_obj=agent, obj_in={"status": "stopped"})
    # Then logical routing is revoked without pretending physical death.
    async with transaction(f.db.second):
        row = await get_personal(owner.agent_id)
        assert row is not None
        assert row.generation != owner.generation
        assert row.state == "disconnecting"
        assert row.account_id == owner.account_id
        assert not row.purge_requested
    async with transaction(f.db.first):
        await agent_dao.update(db_obj=stopped, obj_in={"creator_id": f.sender_id})
    async with transaction(f.db.second):
        row = await get_personal(owner.agent_id)
        assert row is not None
        assert row.purge_requested
        assert row.account_id == owner.account_id


def pending_connection() -> PersonalConnection:
    return PersonalConnection(
        config_id=uuid4(),
        agent_id=uuid4(),
        tenant_id=uuid4(),
        owner_id=uuid4(),
        auth_epoch=uuid4(),
        generation=uuid4(),
        attempt_id=uuid4(),
        revision=1,
        state="pending",
        desired=True,
        created_at=datetime.now(UTC),
    )


async def stop_supervisor(task: asyncio.Task[None]) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


@pytest.mark.parametrize("failing_stage", ["claim", "run_child"])
async def test_supervisor_contains_a_failing_connection_and_respawns_it_after_the_cooldown(monkeypatch, failing_stage):
    # Given one connection that raises an unclassified error and a healthy sibling.
    monkeypatch.setattr(supervisor, "_POLL_SECONDS", 0.01)
    monkeypatch.setattr(supervisor, "_COOLDOWN_SECONDS", 0.2)
    bad, good = pending_connection(), pending_connection()
    claims: dict[object, list[float]] = {bad.config_id: [], good.config_id: []}
    retried, sibling_cancelled = asyncio.Event(), asyncio.Event()
    hold = asyncio.Event()

    async def candidates():
        return [bad, good]

    async def claim(config_id):
        claims[config_id].append(monotonic())
        if config_id == bad.config_id and failing_stage == "claim" and len(claims[config_id]) == 1:
            raise RuntimeError("claim exploded")
        if config_id == bad.config_id and len(claims[config_id]) == 2:
            retried.set()
        return bad if config_id == bad.config_id else good

    async def run_child(connection, _command):
        if connection is bad and failing_stage == "run_child" and len(claims[bad.config_id]) == 1:
            raise RuntimeError("child exploded")
        try:
            await hold.wait()
        except asyncio.CancelledError:
            if connection is good:
                sibling_cancelled.set()
            raise

    monkeypatch.setattr(supervisor, "candidates", candidates)
    monkeypatch.setattr(supervisor, "claim", claim)
    monkeypatch.setattr(supervisor, "run_child", run_child)
    # When the supervisor runs until the failing connection has been respawned.
    task = asyncio.create_task(supervisor.personal_supervisor())
    await asyncio.wait_for(retried.wait(), 5)
    # Then the supervisor and the sibling survive, and the respawn waited out the cooldown.
    assert not task.done()
    assert not sibling_cancelled.is_set()
    assert len(claims[good.config_id]) == 1
    first, second = claims[bad.config_id][:2]
    assert second - first >= 0.2
    await stop_supervisor(task)


async def test_supervisor_backs_off_when_candidates_fails(monkeypatch):
    # Given a candidates query that fails twice before recovering.
    monkeypatch.setattr(supervisor, "_POLL_SECONDS", 0.01)
    monkeypatch.setattr(supervisor, "_BACKOFF_START_SECONDS", 0.05)
    monkeypatch.setattr(supervisor, "_BACKOFF_MAX_SECONDS", 0.1)
    connection = pending_connection()
    calls: list[float] = []
    spawned = asyncio.Event()

    async def candidates():
        calls.append(monotonic())
        if len(calls) <= 2:
            raise RuntimeError("database unavailable")
        return [connection]

    async def claim(_config_id):
        return connection

    async def run_child(_connection, _command):
        spawned.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(supervisor, "candidates", candidates)
    monkeypatch.setattr(supervisor, "claim", claim)
    monkeypatch.setattr(supervisor, "run_child", run_child)
    # When the supervisor runs.
    task = asyncio.create_task(supervisor.personal_supervisor())
    await asyncio.wait_for(spawned.wait(), 5)
    # Then the failure never escapes, each retry waits out the growing backoff, and the connection is spawned.
    assert not task.done()
    assert calls[1] - calls[0] >= 0.05
    assert calls[2] - calls[1] >= 0.1
    await stop_supervisor(task)
