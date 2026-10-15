import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api import background_tasks, gateway
from app.dao.agent_dao import agent_dao
from app.db import session
from app.records.agent import AgentRecord
from app.schemas.schemas import GatewayReportRequest, GatewaySendMessageRequest
from app.services import openclaw_hot_cache, openclaw_routing


def _agent():
    return AgentRecord(id=uuid4(), name="Guest", creator_id=uuid4(), tenant_id=uuid4(), container_id="fake")


@pytest.mark.parametrize("reason", ["stopped", "expired", "elapsed", "disabled_tenant"])
async def test_hot_cache_never_authorizes_revoked_gateway_key(monkeypatch, reason):
    # Given: a formerly allowed cached key whose authoritative DB lookup now denies it.
    agent = _agent()
    monkeypatch.setattr(openclaw_hot_cache, "get_cached_agent_by_key", lambda key: agent)
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(agent_dao, "get_openclaw_by_api_key", lookup)
    # When / Then: every denial still reaches the database, even with a hot cache.
    with pytest.raises(HTTPException) as exc:
        await gateway._get_agent_by_key(reason)
    assert exc.value.status_code == 401
    lookup.assert_awaited_once_with(reason)


async def test_gateway_key_sql_rejects_all_persisted_stop_reasons(monkeypatch):
    # Given: a DAO session that records its parameterized authorization query.
    db = SimpleNamespace(fetchone=AsyncMock(return_value=None))

    @asynccontextmanager
    async def context():
        yield db

    monkeypatch.setattr(agent_dao, "session", context)
    # When: authenticating a key.
    assert await agent_dao.get_openclaw_by_api_key("secret") is None
    # Then: all persisted revocations participate in the SQL decision.
    sql, params = db.fetchone.await_args.args
    assert "status <> 'stopped'" in sql
    assert "is_expired IS FALSE" in sql
    assert "expires_at" in sql
    assert "clock_timestamp()" in sql
    assert "tenants" in sql
    assert "is_active IS TRUE" in sql
    assert params["key"] != "secret"


@pytest.mark.parametrize("status", ["idle", "creating", "error"])
async def test_poll_promotes_even_when_last_seen_is_throttled(monkeypatch, status):
    # Given: an externally hosted guest in a promotable state and a warm last-seen throttle.
    agent = replace(_agent(), status=status)
    monkeypatch.setattr(gateway, "_get_agent_by_key", AsyncMock(return_value=agent))
    monkeypatch.setattr(openclaw_hot_cache, "should_touch_last_seen", lambda aid: False)
    promote = AsyncMock(return_value=replace(agent, status="running"))
    monkeypatch.setattr(agent_dao, "touch_openclaw", promote, raising=False)
    monkeypatch.setattr(gateway, "claim_poll_batch", AsyncMock(return_value=[]))
    monkeypatch.setattr(gateway, "_poll_relationships", AsyncMock(return_value=[]))

    @asynccontextmanager
    async def context():
        yield None

    monkeypatch.setattr(gateway, "connection_ctx", context, raising=False)
    # When: the guest polls.
    await gateway.poll_messages("key")
    # Then: promotion is not lost to the timestamp throttle.
    promote.assert_awaited_once()
    assert promote.await_args is not None
    assert promote.await_args.kwargs["touch_last_seen"] is False


async def test_promotion_lock_wait_rereads_stop_before_claim(monkeypatch):
    # Given: a stop transaction holds the tenant mutex while poll starts with an old record.
    agent = _agent()
    mutex = asyncio.Lock()
    await mutex.acquire()
    waiting = asyncio.Event()
    persisted_status = "idle"
    queries = []

    async def fetchone(sql, params):
        queries.append(sql)
        if sql.startswith("SELECT id FROM tenants"):
            waiting.set()
            await mutex.acquire()
            return {"id": agent.tenant_id}
        if sql.startswith("SELECT tenant_id"):
            return {"tenant_id": agent.tenant_id}
        assert "status <> 'stopped'" in sql
        return None if persisted_status == "stopped" else asdict(agent)

    @asynccontextmanager
    async def context():
        try:
            yield SimpleNamespace(fetchone=fetchone)
        finally:
            mutex.release()

    @asynccontextmanager
    async def poll_context():
        yield None

    monkeypatch.setattr(agent_dao, "session", context)
    monkeypatch.setattr(gateway, "connection_ctx", poll_context)
    monkeypatch.setattr(gateway, "_get_agent_by_key", AsyncMock(return_value=agent))
    claim = AsyncMock(return_value=[])
    monkeypatch.setattr(gateway, "claim_poll_batch", claim)
    # When: the stop commits while the poll is waiting for the same mutex.
    poll = asyncio.create_task(gateway.poll_messages("key"))
    await waiting.wait()
    persisted_status = "stopped"
    mutex.release()
    with pytest.raises(HTTPException) as exc:
        await poll
    # Then: the conditional write denies the old snapshot and no work is claimed.
    assert exc.value.status_code == 401
    claim.assert_not_awaited()
    assert "FROM tenants" in queries[0]
    assert "FROM agents" in queries[1]


@pytest.mark.parametrize("operation", ["send", "report"])
@pytest.mark.parametrize("touch_last_seen", [False, True])
async def test_stop_winning_lock_denies_send_and_report(monkeypatch, operation, touch_last_seen):
    # Given: auth saw the old snapshot while a stop transaction owns the tenant lock.
    agent = _agent()
    mutex = asyncio.Lock()
    await mutex.acquire()
    waiting = asyncio.Event()
    stopped = False

    async def wait_for_stop():
        waiting.set()
        await mutex.acquire()

    async def fetchone(sql, params):
        if sql.startswith("SELECT id FROM tenants"):
            await wait_for_stop()
            return {"id": agent.tenant_id}
        if sql.startswith("SELECT tenant_id"):
            return {"tenant_id": agent.tenant_id}
        return None if stopped else asdict(agent)

    @asynccontextmanager
    async def dao_context():
        try:
            yield SimpleNamespace(fetchone=fetchone)
        finally:
            mutex.release()

    @asynccontextmanager
    async def request_context():
        yield None

    async def old_touch(**kwargs):
        await wait_for_stop()
        mutex.release()
        return kwargs["db_obj"]

    async def old_report(*args):
        await wait_for_stop()
        mutex.release()

    update = AsyncMock(side_effect=old_touch)
    record = AsyncMock(side_effect=old_report)
    publish = AsyncMock()
    monkeypatch.setattr(agent_dao, "session", dao_context)
    monkeypatch.setattr(agent_dao, "update", update)
    monkeypatch.setattr(gateway, "connection_ctx", request_context)
    monkeypatch.setattr(gateway, "_get_agent_by_key", AsyncMock(return_value=agent))
    monkeypatch.setattr(gateway, "record_report", record)
    monkeypatch.setattr(gateway, "publish_report", publish)
    monkeypatch.setattr(gateway.gateway_message_dao, "get_for_agent", AsyncMock(return_value=None))
    monkeypatch.setattr(gateway.agent_agent_relationship_dao, "list_for_agent_with_targets", AsyncMock(return_value=[]))
    monkeypatch.setattr(gateway.agent_relationship_dao, "list_for_agent_with_members", AsyncMock(return_value=[]))
    monkeypatch.setattr(openclaw_hot_cache, "should_touch_last_seen", lambda _id: touch_last_seen)
    # When: stop commits before the request can acquire its admission lock.
    request = (
        gateway.send_message(GatewaySendMessageRequest(target="missing", content="hello"), "key")
        if operation == "send"
        else gateway.report_result(GatewayReportRequest(message_id=uuid4(), result="done"), "key")
    )
    task = asyncio.create_task(request)
    await asyncio.wait_for(waiting.wait(), timeout=1)
    stopped = True
    mutex.release()
    with pytest.raises(HTTPException) as exc:
        await task
    # Then: no last-seen write, report claim or publisher runs after revocation wins.
    assert exc.value.status_code == 401
    update.assert_not_awaited()
    record.assert_not_awaited()
    publish.assert_not_awaited()


@pytest.mark.parametrize("rollback", [False, True])
async def test_enqueued_wake_uses_only_committed_clean_context(monkeypatch, rollback):
    # Given: an enqueue within the actual owning transaction, with guest I/O faked.
    agent = _agent()
    raw = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())

    @asynccontextmanager
    async def connection():
        yield raw

    monkeypatch.setattr(session, "get_pool", lambda: SimpleNamespace(connection=connection))
    observations = []

    async def wake(*args):
        observations.append((raw.commit.await_count, session.get_connection()))

    monkeypatch.setattr(openclaw_routing, "_run_inbox_wake", wake)
    # When: the owner commits or rolls back after scheduling a wake.
    try:
        async with session.connection_ctx():
            openclaw_routing._schedule_inbox_wake(agent, "hello")
            if rollback:
                raise RuntimeError("rollback")
    except RuntimeError:
        pass
    await asyncio.gather(*background_tasks._tasks)
    await asyncio.gather(*openclaw_routing._wake_tasks)
    # Then: rollback dispatches nothing; commit dispatches with no DB connection bound.
    assert observations == ([] if rollback else [(1, None)])


@pytest.mark.parametrize("status", ["idle", "creating", "error"])
async def test_nonpoll_admission_preserves_status_and_last_seen(monkeypatch, status):
    # Given: a valid guest that has not sent a poll or heartbeat.
    agent = replace(_agent(), status=status, openclaw_last_seen=None)
    queries = []

    async def fetchone(sql, params):
        queries.append(sql)
        if sql.startswith("SELECT id FROM tenants"):
            return {"id": agent.tenant_id}
        if sql.startswith("SELECT tenant_id"):
            return {"tenant_id": agent.tenant_id}
        return asdict(agent)

    @asynccontextmanager
    async def context():
        yield SimpleNamespace(fetchone=fetchone)

    monkeypatch.setattr(agent_dao, "session", context)
    # When: send/report admission locks and freshly authorizes the guest.
    admitted = await agent_dao.lock_openclaw(agent)
    # Then: admission itself does not promote or touch a duplicate report's timestamp.
    assert admitted is not None
    assert admitted.status == status
    assert admitted.openclaw_last_seen is None
    assert all(sql.startswith("SELECT") for sql in queries)
    assert "status <> 'stopped'" in queries[-1]
