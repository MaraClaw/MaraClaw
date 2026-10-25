import asyncio
import threading
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Unpack
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app.api import background_tasks, gateway
from app.core.domain.agents import AgentStatus
from app.core.domain.users import UserRole
from app.dao.agent_dao import agent_dao
from app.db import session
from app.records.agent import AgentRecord
from app.records.tenant import TenantRecord
from app.records.user import UserRecord
from app.schemas.schemas import GatewayReportRequest, GatewaySendMessageRequest
from app.services import (
    admin_provisioning,
    agent_orphans,
    agent_stops,
    company_transfer_cleanup,
    heartbeat,
    openclaw_hot_cache,
    openclaw_routing,
    quota_guard,
    tenant_lifecycle,
)
from app.services.agent_manager import agent_manager
from builtin_keyword_fakes import IncludeAllKeyword


@pytest.fixture(autouse=True)
def _isolated_workspace_locks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(agent_manager, "_agent_dir", lambda agent_id: tmp_path / str(agent_id))
    monkeypatch.setattr(company_transfer_cleanup, "reconcile_transfer_cleanup", AsyncMock())


def _agent() -> AgentRecord:
    return AgentRecord(id=uuid4(), name="Guest", creator_id=uuid4(), tenant_id=uuid4(), container_id="fake")


async def test_reconcile_sweeps_only_old_labeled_orphans_on_this_deployment_network(monkeypatch):
    now = datetime.now(UTC)
    existing = _agent()
    orphan_id = uuid4()
    calls = []
    loop_thread = threading.get_ident()

    def container(
        name: str, label: str | None, network: str | None = "owned", age: timedelta = timedelta(minutes=5)
    ) -> SimpleNamespace:
        return SimpleNamespace(
            id=name,
            config=SimpleNamespace(labels={"maraclaw.agent_id": label} if bool(label) else None),
            network_settings=SimpleNamespace(networks={network: {}} if network is not None else None),
            created=now - age,
        )

    listed = [
        container("orphan", str(orphan_id)),
        container("retained", str(existing.id)),
        container("unlabeled", None),
        container("foreign", str(uuid4()), network="other"),
        container("young", str(uuid4()), age=timedelta(seconds=30)),
        container("malformed", "not-a-uuid"),
        container("detached", str(uuid4()), network=None),
    ]

    class Containers:
        def list(self, *, filters, **kwargs: Unpack[IncludeAllKeyword]):
            assert kwargs["all"] is True
            assert filters == {"label": "maraclaw.agent_id", "network": "owned"}
            assert threading.get_ident() != loop_thread
            return listed

        def inspect(self, reference: str) -> SimpleNamespace:
            assert threading.get_ident() != loop_thread
            return {"fake": listed[1], "retained": listed[1]}[reference]

        def stop(self, container_id, *, time):
            assert threading.get_ident() != loop_thread
            calls.append(("stop", container_id))

        def remove(self, container_ids):
            assert threading.get_ident() != loop_thread
            calls.append(("remove", container_ids))

    monkeypatch.setattr(agent_manager, "docker_client", SimpleNamespace(container=Containers()))
    monkeypatch.setattr(agent_orphans, "get_settings", lambda: SimpleNamespace(DOCKER_NETWORK="owned"))
    monkeypatch.setattr(agent_dao, "prepare_container_stops", AsyncMock(return_value=[]))
    lookup = AsyncMock(return_value=[existing])
    monkeypatch.setattr(agent_dao, "get_many", lookup)
    monkeypatch.setattr(agent_dao, "get", AsyncMock(return_value=replace(existing, status="stopped")))
    monkeypatch.setattr(agent_orphans, "assert_no_active_run", AsyncMock())
    await agent_stops.reconcile_stopped_containers(now)
    assert calls == [("stop", "orphan"), ("remove", ["orphan"])]
    assert lookup.await_args is not None
    assert set(lookup.await_args.args[0]) == {existing.id, orphan_id}


async def test_orphan_sweep_continues_after_one_docker_failure(monkeypatch):
    now = datetime.now(UTC)
    calls = []

    class Containers:
        def list(self, **kwargs):
            return [
                SimpleNamespace(
                    id=name,
                    config=SimpleNamespace(labels={"maraclaw.agent_id": str(uuid4())}),
                    network_settings=SimpleNamespace(networks={"owned": {}}),
                    created=now - timedelta(minutes=5),
                )
                for name in ("broken", "healthy")
            ]

        def stop(self, container_id, **kwargs):
            if container_id == "broken":
                raise OSError("Docker unavailable")

        def remove(self, container_ids):
            calls.extend(container_ids)

    monkeypatch.setattr(agent_manager, "docker_client", SimpleNamespace(container=Containers()))
    monkeypatch.setattr(agent_orphans, "get_settings", lambda: SimpleNamespace(DOCKER_NETWORK="owned"))
    monkeypatch.setattr(agent_dao, "get_many", AsyncMock(return_value=[]))
    await agent_orphans.sweep_orphan_containers(now)
    assert calls == ["healthy"]


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


@pytest.mark.parametrize("rollback", [False, True])
@pytest.mark.parametrize("scope", ["user", "tenant"])
async def test_disable_stops_only_after_owning_crud_commit(monkeypatch, rollback, scope):
    # Given: the real request transaction owner, with only DB writes and Docker faked.
    agent = _agent()
    commit = AsyncMock()
    raw = SimpleNamespace(commit=commit, rollback=AsyncMock())

    async def current(agent_id: UUID) -> AgentRecord:
        assert agent_id == agent.id
        assert commit.await_count == 1
        assert session.get_connection() is None
        return replace(agent, status="stopped")

    @asynccontextmanager
    async def connection():
        yield raw

    monkeypatch.setattr(session, "get_pool", lambda: SimpleNamespace(connection=connection))
    monkeypatch.setattr(agent_dao, "get", current)
    monkeypatch.setattr(admin_provisioning, "write_admin_audit", AsyncMock())
    for dao, method, result in (
        (admin_provisioning.user_dao, "update", SimpleNamespace(is_active=False)),
        (agent_dao, "disable_for_creator", [agent]),
        (agent_dao, "disable_for_tenant", 1),
        (agent_dao, "list_for_tenant", [replace(agent, status="stopped")]),
        (tenant_lifecycle.tenant_dao, "update", None),
        (admin_provisioning.user_dao, "deactivate_for_tenant", 1),
    ):
        monkeypatch.setattr(dao, method, AsyncMock(return_value=result))
    for dao in (admin_provisioning.agent_trigger_dao, admin_provisioning.agent_schedule_dao):
        monkeypatch.setattr(dao, "disable_for_creator", AsyncMock())
        monkeypatch.setattr(dao, "disable_for_tenant", AsyncMock())
    observed = []

    async def stop(record):
        observed.append((record.container_id, raw.commit.await_count, session.get_connection()))
        return True

    monkeypatch.setattr(agent_manager, "stop_container", stop)
    owner = session.bind_crud_connection()
    assert isinstance(owner, AsyncGenerator)
    await anext(owner)
    try:
        # When: disable joins that still-open request transaction.
        if scope == "user":
            actor = UserRecord(id=uuid4(), role=UserRole.PLATFORM_ADMIN)
            target = UserRecord(id=agent.creator_id, tenant_id=agent.tenant_id, role=UserRole.MEMBER, is_active=True)
            await admin_provisioning.set_end_user_active(actor=actor, target=target, is_active=False)
        else:
            tenant = TenantRecord(id=uuid4(), name="Company", slug="company", created_at=datetime.now(UTC))
            await tenant_lifecycle.set_tenant_active(tenant, is_active=False)
        assert observed == []
    finally:
        if rollback:
            with pytest.raises(RuntimeError, match="rollback"):
                await owner.athrow(RuntimeError("rollback"))
        else:
            with pytest.raises(StopAsyncIteration):
                await anext(owner)
        if background_tasks._tasks:
            await asyncio.gather(*tuple(background_tasks._tasks))
    # Then: only a committed disable stops, in a clean context (including already-stopped rows).
    assert observed == ([] if rollback else [("fake", 1, None)])


async def test_heartbeat_reconciles_before_listing_heartbeat_candidates(monkeypatch):
    # Given: no heartbeat-enabled agents, but a reconciler with pending expiry work.
    calls = []

    async def reconcile(now):
        calls.append("reconcile")

    async def candidates():
        calls.append("candidates")
        return []

    monkeypatch.setattr("app.services.agent_stops.reconcile_stopped_containers", reconcile)
    monkeypatch.setattr(agent_dao, "list_heartbeat_candidates", candidates)
    # When: the worker ticks without any guest traffic.
    await heartbeat._heartbeat_tick()
    # Then: reconciliation is independent of heartbeat eligibility.
    assert calls == ["reconcile", "candidates"]


async def test_actual_docker_stop_runs_off_event_loop(monkeypatch):
    # Given: a harmless synchronous Docker fake that records the executing thread.
    threads = []
    loop_thread = threading.get_ident()
    agent = _agent()
    owned = SimpleNamespace(
        id=agent.container_id,
        name=f"maraclaw-agent-{agent.id}",
        config=SimpleNamespace(labels={"maraclaw.agent_id": str(agent.id)}),
        host_config=SimpleNamespace(port_bindings={}),
    )
    monkeypatch.setattr(
        agent_manager,
        "docker_client",
        SimpleNamespace(
            container=SimpleNamespace(
                inspect=lambda _ref: owned,
                stop=lambda *args, **kwargs: threads.append(threading.get_ident()),
            )
        ),
    )
    # When: using the public async stop API.
    assert await agent_manager.stop_container(agent) is True
    # Then: blocking Docker work did not execute on the event-loop thread.
    assert len(threads) == 1
    assert threads[0] != loop_thread


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


async def test_expired_heartbeat_disabled_container_retries_false_stop_after_commit(monkeypatch):
    # Given: persisted expiry with heartbeat disabled; Docker fails its first attempt.
    agent = replace(_agent(), heartbeat_enabled=False, expires_at=datetime(2020, 1, 1, tzinfo=UTC))
    events = []
    queries = []

    async def current(agent_id: UUID) -> AgentRecord:
        assert agent_id == agent.id
        assert events[-1] == "commit"
        return replace(agent)

    async def fetchall(sql, params=None):
        queries.append(sql)
        if sql.startswith("SELECT tenant_id"):
            return [{"tenant_id": agent.tenant_id}]
        if sql.startswith("UPDATE agents"):
            agent.status, agent.is_expired = AgentStatus.STOPPED, True
            return [{"id": agent.id}]
        return [asdict(agent)]

    @asynccontextmanager
    async def context():
        events.append("transaction")
        yield SimpleNamespace(
            fetchall=fetchall, fetchone=AsyncMock(return_value={"id": agent.tenant_id}), execute=AsyncMock()
        )
        events.append("commit")

    results = iter([False, True])

    async def stop(record):
        assert events[-1] == "commit"
        assert record.is_expired
        assert record.status == "stopped"
        assert not record.heartbeat_enabled
        events.append("stop")
        return next(results)

    monkeypatch.setattr(agent_dao, "session", context)
    monkeypatch.setattr(agent_dao, "get", current)
    monkeypatch.setattr(agent_manager, "stop_container", stop)
    # When: two worker reconciliation passes run without any guest traffic.
    monkeypatch.setattr(agent_manager, "docker_client", None)
    await agent_stops.reconcile_stopped_containers(datetime.now(UTC))
    await agent_stops.reconcile_stopped_containers(datetime.now(UTC))
    # Then: the failed stop remains discoverable, and each attempt follows its commit.
    assert events == ["transaction", "commit", "stop"] * 2
    assert agent.container_id == "fake"
    assert "heartbeat_enabled" not in queries[0]


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


async def test_repeated_quota_expiry_never_calls_docker(monkeypatch):
    # Given: an agent already marked expired by a previous check.
    agent = replace(_agent(), is_expired=True)
    monkeypatch.setattr(agent_dao, "get", AsyncMock(return_value=agent))
    stop = AsyncMock()
    monkeypatch.setattr(agent_manager, "stop_container", stop)
    # When: successive WebSocket messages check the same expiry.
    for _ in range(2):
        with pytest.raises(quota_guard.AgentExpired):
            await quota_guard.check_agent_expired(agent.id)
    # Then: every message is denied without Docker work in the message path.
    stop.assert_not_awaited()


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
