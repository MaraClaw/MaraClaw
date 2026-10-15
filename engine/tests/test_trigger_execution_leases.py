from __future__ import annotations

import asyncio
import re
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Literal, assert_never
from uuid import uuid4

import pytest

from app.dao.trigger_dao import trigger_execution_dao
from app.db import session as session_module
from app.db.session import connection_ctx
from app.records.trigger import AgentTriggerRecord, TriggerExecutionRecord
from app.services.trigger_runtime import dispatch, executions, invoker
from test_crud_perf import _FakeCursor, _FakePool, _FakeRawConnection, _PoolConnectionCM


class TableCursor(_FakeCursor):
    def __init__(self, parent: TableConnection):
        super().__init__(parent)
        self.raw = parent
        self.results = []

    async def execute(self, query, params=None):
        await super().execute(query, params)
        self.raw.pool.events.append((self.raw, query, params, self.raw.commits))
        if not query.lstrip().startswith("UPDATE trigger_executions"):
            return
        # Parse every predicate and assignment before evaluating any row.
        parts = re.fullmatch(
            r"UPDATE trigger_executions\s+SET (.*?)\s+WHERE (.*?)(?:\s+RETURNING id)?\s*",
            query.strip(),
            re.S,
        )
        assert parts, query
        assignments = re.split(r",\s*(?=\w+\s*=)", parts[1])
        predicates = re.split(r"\s+AND\s+", parts[2])

        def value(expr, row):
            if expr == "NULL":
                return None
            if match := re.fullmatch(r"%\((\w+)\)s", expr):
                return params[match[1]]
            if match := re.fullmatch(r"'([^']*)'", expr):
                return match[1]
            if match := re.fullmatch(r"COALESCE\((\w+),\s*%\((\w+)\)s\)", expr):
                return row[match[1]] if row[match[1]] is not None else params[match[2]]
            raise AssertionError(f"Unrecognized SET expression: {expr}")

        parsed_sets = []
        for item in assignments:
            match = re.fullmatch(r"(\w+)\s*=\s*(.*)", item.strip())
            assert match, item
            # Validate even when the table is empty or every predicate fails.
            _ = value(match[2], dict.fromkeys(self.raw.pool.columns))
            parsed_sets.append((match[1], match[2]))
        parsed_where = []
        for item in predicates:
            match = re.fullmatch(r"(\w+) = ANY\(%\((\w+)\)s\)", item.strip())
            if match:
                parsed_where.append((match[1], params[match[2]], True))
                continue
            match = re.fullmatch(r"(\w+) = (%\(\w+\)s|'[^']*')", item.strip())
            assert match, f"Unrecognized predicate: {item}"
            parsed_where.append((match[1], value(match[2], {}), False))
        changed = []
        for row in self.raw.pool.rows.values():
            if all(row[col] in expected if many else row[col] == expected for col, expected, many in parsed_where):
                for col, expr in parsed_sets:
                    row[col] = value(expr, row)
                changed.append({"id": row["id"]})
        self.results = changed if "RETURNING id" in query else []
        if "SET lease_expires_at" in query:
            self.raw.pool.renewals += 1
            if self.raw.pool.renewals >= self.raw.pool.target:
                self.raw.pool.renewed.set()

    async def fetchall(self):
        return self.results


class TableConnection(_FakeRawConnection):
    def __init__(self, pool):
        super().__init__()
        self.pool = pool

    def cursor(self):
        return TableCursor(self)


class TableCM(_PoolConnectionCM):
    def __init__(self, raw: TableConnection):
        super().__init__(raw)
        self.raw = raw

    async def __aenter__(self):
        self.raw.pool.open += 1
        return await super().__aenter__()

    async def __aexit__(self, *args):
        self.raw.pool.open -= 1
        return await super().__aexit__(*args)


class TablePool(_FakePool):
    def __init__(self):
        self.rows = {}
        self.columns = asdict(TriggerExecutionRecord(uuid4(), uuid4(), uuid4())).keys()
        self.connections = []
        self.events = []
        self.open = 0
        self.renewals = 0
        self.target = 2
        self.renewed = asyncio.Event()
        super().__init__(TableConnection(self))

    def connection(self):
        raw = TableConnection(self)
        self.connections.append(raw)
        return TableCM(raw)


@pytest.fixture
def table(monkeypatch):
    pool = TablePool()
    monkeypatch.setattr(session_module, "get_pool", lambda: pool)
    token = session_module._conn_ctx.set(None)
    monkeypatch.setattr(executions, "LEASE_RENEW_INTERVAL", timedelta(milliseconds=10), raising=False)
    yield pool
    session_module._conn_ctx.reset(token)


def queued(table, *, started=False):
    trigger = AgentTriggerRecord(uuid4(), uuid4(), name="research", type="once")
    execution = TriggerExecutionRecord(
        uuid4(), trigger.id, trigger.agent_id, started_at=datetime.now(UTC) if started else None
    )
    table.rows[execution.id] = asdict(execution)
    return execution, trigger


def claim_snapshots(monkeypatch, table, pairs):
    async def claim(**_kwargs):
        return [(TriggerExecutionRecord(**table.rows[e.id]), replace(t)) for e, t in pairs]

    monkeypatch.setattr(trigger_execution_dao, "claim_pending", claim)


async def claimed_lease(monkeypatch, table):
    pair = queued(table)
    claim_snapshots(monkeypatch, table, [pair])
    rows = await executions.claim_pending_trigger_executions()
    trigger = executions.build_execution_runtime_trigger(rows[0][1], rows[0][0])
    return executions.trigger_execution_leases([trigger])[0], trigger


@pytest.mark.asyncio
async def test_takeover_fences_stale_owner(table, monkeypatch):
    monkeypatch.setattr(executions.settings, "INSTANCE_ID", "same-worker" * 20)
    stale, trigger = await claimed_lease(monkeypatch, table)
    row = table.rows[stale.execution_ids[0]]
    row["lease_expires_at"] = datetime.now(UTC) - timedelta(seconds=1)
    reclaimed = await executions.claim_pending_trigger_executions()
    current = executions.trigger_execution_leases(
        [executions.build_execution_runtime_trigger(reclaimed[0][1], reclaimed[0][0])]
    )[0]
    before = dict(row)
    assert current.token != stale.token
    assert len(current.token) <= 128
    assert await executions.renew_trigger_execution_lease(stale) == []
    assert await executions.mark_trigger_executions_completed([stale]) == []
    assert await executions.mark_trigger_executions_failed([stale], "old failure") == []
    assert row == before
    assert await executions.mark_trigger_executions_completed([current]) == list(current.execution_ids)
    assert row["status"] == "completed"
    assert row["lease_owner"] is row["lease_expires_at"] is None
    row["lease_owner"] = current.token
    before = dict(row)
    assert await executions.renew_trigger_execution_lease(current) == []
    assert await executions.mark_trigger_executions_failed([current], "late") == []
    assert row == before
    assert executions.trigger_execution_leases([AgentTriggerRecord(uuid4(), trigger.agent_id)]) == []


@pytest.mark.asyncio
async def test_first_starts_count_inside_claim_transaction(table, monkeypatch):
    first, reclaim = queued(table), queued(table, started=True)
    claim_snapshots(monkeypatch, table, [first, reclaim])
    await executions.claim_pending_trigger_executions()
    raw = table.connections[0]
    sql = [(q, p) for q, p in raw.executed]
    updates = [(q, p) for q, p in sql if q.lstrip().startswith("UPDATE agent_triggers")]
    assert len(updates) == 1
    assert updates[0][1]["ids"] == [first[0].id]
    assert not any("FOR NO KEY UPDATE OF t" in q and "SKIP LOCKED" not in q for q, _ in sql)
    assert all(commits == 0 for conn, _q, _p, commits in table.events if conn is raw)
    assert raw.commits == 1
    assert sum(q.lstrip().startswith("UPDATE trigger_executions") for q, _ in sql) == 1
    await executions.claim_pending_trigger_executions()
    assert not any(q.lstrip().startswith("UPDATE agent_triggers") for q, _ in table.connections[1].executed)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["renew", "completed", "failed"])
async def test_lease_writes_commit_each_execution_separately(table, operation: Literal["renew", "completed", "failed"]):
    # Given two processing rows and an ambient parent transaction.
    ids = tuple(queued(table, started=True)[0].id for _ in range(2))
    lease = executions.ExecutionLease("owner", ids)
    for execution_id in ids:
        table.rows[execution_id].update(status="processing", lease_owner=lease.token)
    async with connection_ctx():
        parent = table.connections[0]
        # When writing the lease through each public service.
        match operation:
            case "renew":
                changed = await executions.renew_trigger_execution_lease(lease)
            case "completed":
                changed = await executions.mark_trigger_executions_completed([lease])
            case "failed":
                changed = await executions.mark_trigger_executions_failed([lease], "failed invocation")
            case unreachable:
                assert_never(unreachable)
        # Then each row has its own committed checkout, outside the parent.
        assert changed == list(ids)
        assert not parent.executed
        children = table.connections[1:]
        assert len(children) == 2
        for raw, execution_id in zip(children, ids, strict=True):
            assert raw.commits == 1
            assert len(raw.executed) == 1
            query, params = raw.executed[0]
            assert query.lstrip().startswith("UPDATE trigger_executions")
            assert "WHERE id = %(id)s" in query
            assert params["id"] == execution_id
        assert parent.commits == 0


@pytest.mark.asyncio
async def test_lease_write_cancellation_joins_child_and_preserves_trace(table, monkeypatch):
    from app.core.logging import get_trace_id, set_trace_id

    entered, cleaning, release, joined = (asyncio.Event() for _ in range(4))
    lease = executions.ExecutionLease("owner", (uuid4(),))
    caller = None

    async def renew(*_args, **_kwargs):
        assert asyncio.current_task() is not caller
        assert session_module.get_connection() is None
        assert get_trace_id() == "lease-write-trace"
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            joined.set()

    async def write():
        set_trace_id("lease-write-trace")
        async with connection_ctx():
            await executions.renew_trigger_execution_lease(lease)

    monkeypatch.setattr(trigger_execution_dao, "renew_lease", renew)
    caller = asyncio.create_task(write())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        caller.cancel()
        await asyncio.wait_for(cleaning.wait(), 1)
        assert not caller.done()
    finally:
        release.set()
        if not cleaning.is_set():
            caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
    assert joined.is_set()


class Warnings:
    def __init__(self):
        self.messages = []
        self.changed = asyncio.Event()

    def warning(self, message, *args, **kwargs):
        self.messages.append(str(message))
        self.changed.set()


@pytest.mark.asyncio
async def test_heartbeat_isolates_parent_and_joins(table, monkeypatch):
    lease, _ = await claimed_lease(monkeypatch, table)
    baseline = set(asyncio.all_tasks())
    async with connection_ctx():
        parent = table.connections[-1]
        async with executions.hold_trigger_execution_leases([lease]):
            await asyncio.wait_for(table.renewed.wait(), 1)
            assert table.open == 1
            assert not parent.executed
            assert all(c.commits == 1 for c in table.connections if c is not parent)
    assert set(asyncio.all_tasks()) == baseline
    count = table.renewals
    await asyncio.sleep(0.025)
    assert table.renewals == count
    assert table.open == 0


@pytest.mark.asyncio
async def test_partial_lease_loss_stops_invocation_and_fails_remaining_owned_rows(table, monkeypatch):
    lease, trigger = await claimed_lease(monkeypatch, table)
    second, _ = queued(table, started=True)
    row = table.rows[second.id]
    row.update(status="processing", lease_owner=lease.token)
    second_trigger = replace(trigger, config={**trigger.config, "_execution_id": str(second.id)})
    cancelled = asyncio.Event()
    before = None

    async def llm(**_kwargs):
        nonlocal before
        row["lease_owner"] = "takeover"
        before = dict(row)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    fake_invoker(monkeypatch, trigger, llm)
    messages = record_messages(monkeypatch)
    await asyncio.wait_for(invoker.invoke_agent_for_triggers(trigger.agent_id, [trigger, second_trigger]), 1)
    assert cancelled.is_set()
    assert row == before
    owned = table.rows[lease.execution_ids[0]]
    assert owned["status"] == "failed"
    assert owned["last_error"] == executions.LEASE_LOST_ERROR
    assert "assistant" not in messages


def fake_invoker(monkeypatch, trigger, llm):
    from app.services import audit_logger, enterprise_llm, llm as llm_module

    agent = SimpleNamespace(
        id=trigger.agent_id,
        is_expired=False,
        primary_model_id=uuid4(),
        name="research",
        creator_id=uuid4(),
        role_description="",
        tenant_id=uuid4(),
    )

    async def get_agent(_id):
        return agent

    async def get_model(_id):
        return SimpleNamespace(id=agent.primary_model_id)

    async def participant(*_args):
        return None

    async def create(**_kwargs):
        return SimpleNamespace(id=uuid4())

    async def nothing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(invoker.agent_dao, "get", get_agent)
    monkeypatch.setattr(invoker.llm_model_dao, "get", get_model)
    monkeypatch.setattr(enterprise_llm, "model_usable_in_tenant", lambda *_args: True)
    monkeypatch.setattr(invoker.participant_dao, "get_by_type_ref", participant)
    monkeypatch.setattr(invoker.chat_session_dao, "create", create)
    monkeypatch.setattr(invoker.chat_message_dao, "insert_message", nothing)
    monkeypatch.setattr(llm_module, "call_llm", llm)
    monkeypatch.setattr(audit_logger, "write_audit_log", nothing)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
async def test_invoker_renews_without_connection_and_finishes_after_join(table, monkeypatch, outcome):
    lease, trigger = await claimed_lease(monkeypatch, table)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def llm(**_kwargs):
        await asyncio.wait_for(table.renewed.wait(), 1)
        assert session_module.get_connection() is None
        assert table.open == 0
        entered.set()
        await release.wait()
        if outcome == "failure":
            raise RuntimeError("model failed")
        return "reply"

    fake_invoker(monkeypatch, trigger, llm)
    original_finish = trigger_execution_dao.finish

    async def finish(*args, **kwargs):
        assert not any("_heartbeat" in str(t.get_coro()) for t in asyncio.all_tasks())
        return await original_finish(*args, **kwargs)

    monkeypatch.setattr(trigger_execution_dao, "finish", finish)
    task = asyncio.create_task(invoker.invoke_agent_for_triggers(trigger.agent_id, [trigger]))
    await asyncio.wait_for(entered.wait(), 1)
    if outcome == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
    else:
        release.set()
        await asyncio.wait_for(task, 1)
    row = table.rows[lease.execution_ids[0]]
    assert row["status"] == {"success": "completed", "failure": "failed", "cancel": "processing"}[outcome]
    assert row["last_error"] == ("model failed" if outcome == "failure" else None)
    assert table.open == 0
    assert not any(t is not asyncio.current_task() for t in asyncio.all_tasks())


@pytest.mark.asyncio
async def test_second_cancellation_waits_for_heartbeat_cleanup(table, monkeypatch):
    _, trigger = await claimed_lease(monkeypatch, table)
    renewing, cleaning, cleanup_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release_cleanup = asyncio.Event()
    original_renew = executions.renew_trigger_execution_lease
    calls = 0

    async def renew(_lease):
        nonlocal calls
        calls += 1
        if calls == 1:
            return await original_renew(_lease)
        renewing.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release_cleanup.wait()
            cleanup_done.set()

    async def llm(**_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(executions, "renew_trigger_execution_lease", renew)
    fake_invoker(monkeypatch, trigger, llm)
    task = asyncio.create_task(invoker.invoke_agent_for_triggers(trigger.agent_id, [trigger]))
    await asyncio.wait_for(renewing.wait(), 1)
    task.cancel()
    await asyncio.wait_for(cleaning.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert cleanup_done.is_set()
    assert all(row["status"] == "processing" for row in table.rows.values())


def record_messages(monkeypatch):
    messages = []

    async def insert(**kwargs):
        messages.append(kwargs["role"])

    monkeypatch.setattr(invoker.chat_message_dao, "insert_message", insert)
    return messages


def short_lease(monkeypatch):
    monkeypatch.setattr(executions, "LEASE_DURATION", timedelta(milliseconds=250))
    monkeypatch.setattr(executions, "LEASE_STOP_MARGIN", timedelta(milliseconds=150), raising=False)


@pytest.mark.asyncio
async def test_entry_confirmation_rejects_lost_lease_before_invocation(table, monkeypatch):
    lease, trigger = await claimed_lease(monkeypatch, table)
    row = table.rows[lease.execution_ids[0]]
    row["lease_owner"] = "takeover"
    before = dict(row)
    started = asyncio.Event()

    async def llm(**_kwargs):
        started.set()
        return "stale reply"

    fake_invoker(monkeypatch, trigger, llm)
    messages = record_messages(monkeypatch)
    baseline = set(asyncio.all_tasks())
    await invoker.invoke_agent_for_triggers(trigger.agent_id, [trigger])
    assert not started.is_set()
    assert not messages
    assert row == before
    assert set(asyncio.all_tasks()) == baseline


@pytest.mark.asyncio
async def test_heartbeat_retries_transient_error_before_deadline(table, monkeypatch):
    lease, trigger = await claimed_lease(monkeypatch, table)
    short_lease(monkeypatch)
    recovered = asyncio.Event()
    original = executions.renew_trigger_execution_lease
    calls = 0

    async def renew(held):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("transient")
        result = await original(held)
        if calls == 3:
            recovered.set()
        return result

    async def llm(**_kwargs):
        await recovered.wait()
        return "reply"

    monkeypatch.setattr(executions, "renew_trigger_execution_lease", renew)
    fake_invoker(monkeypatch, trigger, llm)
    await asyncio.wait_for(invoker.invoke_agent_for_triggers(trigger.agent_id, [trigger]), 1)
    assert calls >= 4  # Entry, failed heartbeat, recovered heartbeat, post-LLM confirmation.
    assert table.rows[lease.execution_ids[0]]["status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "hung"])
async def test_unconfirmed_lease_stops_before_expiry(table, monkeypatch, failure):
    lease, trigger = await claimed_lease(monkeypatch, table)
    short_lease(monkeypatch)
    original = executions.renew_trigger_execution_lease
    calls = 0
    times = []

    async def renew(held):
        nonlocal calls
        calls += 1
        if calls == 1:
            return await original(held)
        if failure == "exception":
            raise RuntimeError("partition")
        await asyncio.Event().wait()
        return []

    async def llm(**_kwargs):
        times.append(table.rows[lease.execution_ids[0]]["lease_expires_at"])
        try:
            await asyncio.Event().wait()
        finally:
            times.append(datetime.now(UTC))

    monkeypatch.setattr(executions, "renew_trigger_execution_lease", renew)
    fake_invoker(monkeypatch, trigger, llm)
    messages = record_messages(monkeypatch)
    await asyncio.wait_for(invoker.invoke_agent_for_triggers(trigger.agent_id, [trigger]), 1)
    assert times[1] < times[0]
    assert "assistant" not in messages
    assert table.rows[lease.execution_ids[0]]["last_error"] == executions.LEASE_LOST_ERROR


@pytest.mark.asyncio
async def test_lease_deadline_starts_before_renewal_io(table, monkeypatch):
    lease, _ = await claimed_lease(monkeypatch, table)
    short_lease(monkeypatch)
    original = executions.renew_trigger_execution_lease
    loop = asyncio.get_running_loop()
    times = []

    async def slow(held):
        times.append(loop.time())
        release = asyncio.Event()
        loop.call_later(0.04, release.set)
        await release.wait()
        result = await original(held)
        times.append(loop.time())
        return result

    monkeypatch.setattr(executions, "renew_trigger_execution_lease", slow)
    async with executions.hold_trigger_execution_leases([lease]) as hold:
        window = (executions.LEASE_DURATION - executions.LEASE_STOP_MARGIN).total_seconds()
        assert hold.deadline <= times[0] + window
        assert times[1] + window - hold.deadline >= 0.035


@pytest.mark.asyncio
async def test_post_llm_confirmation_prevents_stale_delivery(table, monkeypatch):
    lease, trigger = await claimed_lease(monkeypatch, table)
    monkeypatch.setattr(executions, "LEASE_RENEW_INTERVAL", timedelta(seconds=10))
    row = table.rows[lease.execution_ids[0]]
    before = None

    async def llm(**_kwargs):
        nonlocal before
        row["lease_owner"] = "takeover"
        before = dict(row)
        return "stale reply"

    fake_invoker(monkeypatch, trigger, llm)
    messages = record_messages(monkeypatch)
    await invoker.invoke_agent_for_triggers(trigger.agent_id, [trigger])
    assert "assistant" not in messages
    assert row == before
    assert table.renewals == 2


@pytest.mark.asyncio
async def test_external_cancellation_wins_over_lease_loss(table, monkeypatch):
    lease, trigger = await claimed_lease(monkeypatch, table)
    cleaned = asyncio.Event()
    original = executions.renew_trigger_execution_lease
    calls = 0

    async def renew(held):
        nonlocal calls
        calls += 1
        if calls == 1:
            return await original(held)
        try:
            return []
        finally:
            cleaned.set()

    async def llm(**_kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            owner = asyncio.current_task()
            assert owner is not None
            owner.cancel()  # External cancel ordered after the heartbeat's cancel.

    monkeypatch.setattr(executions, "renew_trigger_execution_lease", renew)
    fake_invoker(monkeypatch, trigger, llm)
    messages = record_messages(monkeypatch)
    task = asyncio.create_task(invoker.invoke_agent_for_triggers(trigger.agent_id, [trigger]))
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert cleaned.is_set()
    assert task.cancelling() == 1
    assert table.rows[lease.execution_ids[0]]["status"] == "processing"
    assert "assistant" not in messages
    assert not any("_heartbeat" in str(t.get_coro()) for t in asyncio.all_tasks())


@pytest.mark.asyncio
async def test_confirm_waits_for_inflight_heartbeat_batch(table, monkeypatch):
    lease, _ = await claimed_lease(monkeypatch, table)
    short_lease(monkeypatch)
    original = executions.renew_trigger_execution_lease
    inflight, release, confirming = (asyncio.Event() for _ in range(3))
    calls = 0

    async def renew(held):
        nonlocal calls
        calls += 1
        result = await original(held)
        if calls == 2:
            inflight.set()
            await release.wait()
        return result

    monkeypatch.setattr(executions, "renew_trigger_execution_lease", renew)
    async with executions.hold_trigger_execution_leases([lease]) as hold:
        await asyncio.wait_for(inflight.wait(), 1)
        previous = hold.deadline

        async def confirm():
            confirming.set()
            await hold.confirm()

        task = asyncio.create_task(confirm())
        try:
            await confirming.wait()
            assert not task.done()
            assert calls == 2
            assert hold.deadline == previous
        finally:
            release.set()
            await task
        remaining = (table.rows[lease.execution_ids[0]]["lease_expires_at"] - datetime.now(UTC)).total_seconds()
        margin = executions.LEASE_STOP_MARGIN.total_seconds()
        assert hold.deadline - asyncio.get_running_loop().time() <= remaining - margin + 0.002


@pytest.mark.asyncio
async def test_empty_lease_hold_is_a_noop():
    baseline = set(asyncio.all_tasks())
    async with executions.hold_trigger_execution_leases([]) as hold:
        await hold.confirm()
        assert set(asyncio.all_tasks()) == baseline


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("entry unavailable"), TimeoutError("entry unavailable")])
async def test_entry_renewal_error_propagates_without_terminal_write(table, monkeypatch, error):
    lease, trigger = await claimed_lease(monkeypatch, table)
    baseline = set(asyncio.all_tasks())

    async def renew(_held):
        raise error

    monkeypatch.setattr(executions, "renew_trigger_execution_lease", renew)
    with pytest.raises(type(error)) as caught:
        await invoker.invoke_agent_for_triggers(trigger.agent_id, [trigger])
    assert caught.value is error
    assert table.rows[lease.execution_ids[0]]["status"] == "processing"
    assert set(asyncio.all_tasks()) == baseline


@pytest.mark.asyncio
@pytest.mark.parametrize("body_exit", ["error", "return"])
async def test_internal_loss_wins_over_body_exit(table, monkeypatch, body_exit):
    lease, _ = await claimed_lease(monkeypatch, table)
    owner = asyncio.current_task()
    assert owner is not None
    baseline = owner.cancelling()

    async def run():
        async with executions.hold_trigger_execution_leases([lease]) as hold:
            hold._fire("lease lost")
            if body_exit == "error":
                raise RuntimeError("body cleanup failed")

    with pytest.raises(executions.ExecutionLeaseLostError):
        await run()
    assert owner.cancelling() == baseline


@pytest.mark.asyncio
async def test_partial_confirmation_does_not_extend_deadline(table, monkeypatch):
    lease, _ = await claimed_lease(monkeypatch, table)
    monkeypatch.setattr(executions, "LEASE_RENEW_INTERVAL", timedelta(seconds=10))
    owner = asyncio.current_task()
    assert owner is not None
    baseline = owner.cancelling()
    async with executions.hold_trigger_execution_leases([lease]) as hold:
        deadline = hold.deadline
        table.rows[lease.execution_ids[0]]["lease_owner"] = "takeover"
        with pytest.raises(executions.ExecutionLeaseLostError):
            await hold.confirm()
        assert hold.deadline == deadline
    assert owner.cancelling() == baseline


@pytest.mark.asyncio
async def test_recovery_sql_preserves_pending_enable_guard(table, monkeypatch):
    await trigger_execution_dao.claim_pending(sources=["once"], now=datetime.now(UTC), limit=10)
    query = table.connections[0].executed[0][0]
    assert "FOR UPDATE OF e SKIP LOCKED" in query
    assert "FOR NO KEY UPDATE OF t SKIP LOCKED" in query
    assert "(e.status = 'pending' AND e.source = ANY(%(sources)s) AND t.is_enabled IS TRUE)" in query
    assert query.count("e.source = ANY") == 1
    assert "OR t.type = 'once'" in query
    assert "OR (t.max_fires IS NOT NULL AND t.fire_count >= t.max_fires)" in query
    # Recovery uses a subquery; capture it with the original recording cursor.
    recording_raw = _FakeRawConnection()
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(recording_raw))
    await executions.fail_abandoned_trigger_executions()
    query, params = recording_raw.executed[0]
    assert "FOR UPDATE OF e SKIP LOCKED" in query
    assert "AND t.is_enabled IS NOT TRUE" in query
    assert "AND t.type <> 'once'" in query
    assert "AND (t.max_fires IS NULL OR t.fire_count < t.max_fires)" in query
    assert params["error_text"] == executions.LEASE_EXPIRED_ERROR


@pytest.mark.asyncio
async def test_dispatch_recovers_first_and_protects_payload(table, monkeypatch):
    execution, trigger = queued(table)
    execution.lease_owner = "trusted"
    execution.payload = {"_execution_id": str(uuid4()), "_lease_token": "untrusted"}
    order = []

    async def fail():
        order.append("recover")
        return []

    async def claim():
        order.append("claim")
        return [(execution, trigger)]

    monkeypatch.setattr(dispatch, "fail_abandoned_trigger_executions", fail, raising=False)
    monkeypatch.setattr(dispatch, "claim_pending_trigger_executions", claim)
    grouped, forced = await dispatch.claim_ready_trigger_invocations(datetime.now(UTC))
    assert order == ["recover", "claim"]
    assert forced == {trigger.agent_id}
    leases = executions.trigger_execution_leases(grouped[trigger.agent_id])
    assert leases == [executions.ExecutionLease("trusted", (execution.id,))]


@pytest.mark.asyncio
async def test_empty_tick_still_claims_and_invokes_in_fresh_context(table, monkeypatch):
    from app.services import trigger_daemon

    _, trigger = await claimed_lease(monkeypatch, table)
    launched = asyncio.Event()

    async def enabled():
        return []

    async def claim(_now):
        return {trigger.agent_id: [trigger]}, {trigger.agent_id}

    async def invoke(_id, _triggers):
        assert session_module.get_connection() is None
        launched.set()

    monkeypatch.setattr(trigger_daemon.agent_trigger_dao, "list_enabled", enabled)
    monkeypatch.setattr(trigger_daemon, "claim_ready_trigger_invocations", claim)
    monkeypatch.setattr(trigger_daemon, "_invoke_agent_for_triggers", invoke)
    monkeypatch.setattr(trigger_daemon, "_last_invoke", {})
    async with connection_ctx():
        await trigger_daemon._tick()
        await asyncio.wait_for(launched.wait(), 1)
        await asyncio.gather(*trigger_daemon._BACKGROUND_TASKS)
        await asyncio.sleep(0)
    assert not trigger_daemon._BACKGROUND_TASKS


@pytest.mark.asyncio
async def test_wake_invocation_starts_in_fresh_context(table, monkeypatch, tmp_path):
    from app.core.logging import get_trace_id, set_trace_id
    from app.services import trigger_daemon
    from app.services.agent_tool_exec.registry import (
        ToolExecutionContext,
        current_execution_context,
        use_execution_context,
    )

    agent_id = uuid4()
    caller_context = ToolExecutionContext(tenant_id="caller-tenant", workspace_root=tmp_path)
    seen = []

    async def invoke(_id, _triggers):
        seen.append((session_module._conn_ctx.get(), current_execution_context(), get_trace_id()))

    monkeypatch.setattr(trigger_daemon, "_invoke_agent_for_triggers", invoke)
    monkeypatch.setattr(trigger_daemon, "_last_invoke", {})
    async with connection_ctx() as caller_connection:
        with use_execution_context(caller_context):
            set_trace_id("caller-trace")
            await trigger_daemon.wake_agent_with_context(agent_id, "msg", skip_dedup=True)
            await asyncio.gather(*trigger_daemon._BACKGROUND_TASKS)
            await asyncio.sleep(0)
            assert session_module._conn_ctx.get() is caller_connection
            assert current_execution_context() is caller_context
            assert get_trace_id() == "caller-trace"
    assert not trigger_daemon._BACKGROUND_TASKS
    assert len(seen) == 1
    connection, execution_context, trace_id = seen[0]
    assert connection is None
    assert execution_context is None
    assert trace_id != "caller-trace"


@pytest.mark.asyncio
@pytest.mark.parametrize("clock", ["monotonic", "wall"])
async def test_synchronous_guard_rejects_expiry_before_heartbeat(table, monkeypatch, clock):
    # Given a held lease whose heartbeat has not had a chance to run.
    lease, _ = await claimed_lease(monkeypatch, table)
    async with executions.hold_trigger_execution_leases([lease]) as hold:
        with monkeypatch.context() as patch:
            if clock == "monotonic":
                patch.setattr(hold.loop, "time", lambda: hold.deadline + 1)
            else:
                patch.setattr(time, "time", lambda: hold.wall_deadline + 1)
            # When admission is checked synchronously after either deadline.
            with pytest.raises(executions.ExecutionLeaseLostError, match="lease not renewed"):
                hold.ensure_held()
        # Then no cancellation was scheduled by the synchronous check.
        assert hold.fired_reason is None


@pytest.mark.asyncio
async def test_renewal_rejects_wall_expiry_during_io_without_publishing(table, monkeypatch):
    # Given a confirmed lease and renewal I/O that crosses the old wall bound.
    lease, _ = await claimed_lease(monkeypatch, table)
    async with executions.hold_trigger_execution_leases([lease]) as hold:
        previous = (hold.deadline, hold.wall_deadline)

        async def renew(held):
            monkeypatch.setattr(time, "time", lambda: previous[1] + 1)
            return list(held.execution_ids)

        monkeypatch.setattr(executions, "renew_trigger_execution_lease", renew)
        # When the renewal finishes after the old wall deadline.
        with pytest.raises(executions.ExecutionLeaseLostError, match="lease not renewed"):
            await hold.confirm()
        # Then neither new deadline is published.
        assert (hold.deadline, hold.wall_deadline) == previous


@pytest.mark.asyncio
@pytest.mark.parametrize("fire_first", [False, True])
async def test_sync_loss_and_heartbeat_loss_both_classify_as_lease_loss(table, monkeypatch, fire_first):
    # Given both a synchronous failure and the heartbeat's cancellation.
    lease, _ = await claimed_lease(monkeypatch, table)
    owner = asyncio.current_task()
    assert owner is not None
    baseline = owner.cancelling()

    async def run():
        async with executions.hold_trigger_execution_leases([lease]) as hold:
            if fire_first:
                hold._fire("heartbeat lost")
            else:
                hold.deadline = hold.loop.time() - 1
            try:
                hold.ensure_held()
            finally:
                if not fire_first:
                    hold._fire("heartbeat lost during exit")

    # When the two failure orders unwind the hold.
    with pytest.raises(executions.ExecutionLeaseLostError):
        await run()
    # Then internal cancellation is consumed, not misclassified as external.
    assert owner.cancelling() == baseline


@pytest.mark.asyncio
async def test_paused_invocation_cannot_start_tool_or_post_llm_effects(table, monkeypatch):
    from app.api import websocket
    from app.services import audit_logger
    from app.services.llm import caller

    # Given an invocation paused without yielding until its lease expires.
    lease, trigger = await claimed_lease(monkeypatch, table)
    short_lease(monkeypatch)
    effects = []

    async def execute(*_args, **_kwargs):
        effects.append("tool")
        return "result"

    async def effect(*_args, **_kwargs):
        effects.append("delivery")

    def pause_past_deadline():
        time.sleep(0.12)

    async def llm(**kwargs):
        pause_past_deadline()
        guard_kwargs = {"side_effect_guard": kwargs["side_effect_guard"]} if "side_effect_guard" in kwargs else {}
        await caller._process_tool_call(
            tc={"id": "paused", "function": {"name": "dummy", "arguments": "{}"}},
            api_messages=[],
            agent_id=trigger.agent_id,
            user_id=None,
            session_id="paused",
            supports_vision=False,
            on_tool_call=kwargs["on_tool_call"],
            full_reasoning_content="",
            allowed_tool_names=set(),
            **guard_kwargs,
        )
        return "stale reply"

    fake_invoker(monkeypatch, trigger, llm)
    messages = record_messages(monkeypatch)
    monkeypatch.setattr(caller, "execute_tool", execute)
    monkeypatch.setattr(websocket.manager, "send_to_user", effect)
    monkeypatch.setattr(audit_logger, "write_audit_log", effect)
    # When the paused owner resumes before the heartbeat timer.
    await invoker.invoke_agent_for_triggers(trigger.agent_id, [trigger])
    # Then admission fails before any tool or reply persistence/delivery.
    assert effects == []
    assert messages == ["user"]
    assert table.rows[lease.execution_ids[0]]["last_error"] == executions.LEASE_LOST_ERROR


@pytest.mark.asyncio
async def test_wall_jump_between_reply_and_delivery_stops_invocation(table, monkeypatch):
    from app.api import websocket
    from app.services import audit_logger

    # Given a wall-clock jump during the post-confirmation participant lookup.
    lease, trigger = await claimed_lease(monkeypatch, table)
    effects = []
    reads = 0
    now = time.time()

    async def participant(*_args):
        nonlocal reads
        reads += 1
        if reads == 2:
            monkeypatch.setattr(time, "time", lambda: now + 1000)

    async def llm(**_kwargs):
        return "reply"

    async def effect(*_args, **_kwargs):
        effects.append("delivery")

    fake_invoker(monkeypatch, trigger, llm)
    monkeypatch.setattr(invoker.participant_dao, "get_by_type_ref", participant)
    monkeypatch.setattr(websocket.manager, "send_to_user", effect)
    monkeypatch.setattr(audit_logger, "write_audit_log", effect)
    messages = record_messages(monkeypatch)
    # When persistence resumes after the jump.
    await invoker.invoke_agent_for_triggers(trigger.agent_id, [trigger])
    # Then no stale reply or delivery starts and the still-owned row is failed.
    assert messages == ["user"]
    assert effects == []
    assert table.rows[lease.execution_ids[0]]["last_error"] == executions.LEASE_LOST_ERROR
