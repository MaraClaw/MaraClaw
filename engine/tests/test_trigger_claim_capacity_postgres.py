"""Real PostgreSQL: trigger claims bounded by agent groups and rows under skip-locked execution and trigger locks.

Opt in with BOOTSTRAP_TEST_DATABASE_URL; each test gets a unique schema that is dropped afterwards.
"""

import asyncio
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from psycopg import sql

from app.core.domain.triggers import TRIGGER_DISABLED_ERROR
from app.core.json_types import uuid_from_row
from app.dao.trigger_dao import agent_trigger_dao, trigger_execution_dao
from app.db.connection import DbConnection, Params, Row
from app.records.trigger import AgentTriggerRecord, TriggerExecutionRecord
from app.services import trigger_daemon
from app.services.trigger_runtime import dispatch, executions
from test_openclaw_update_postgres import agent, db, member, tenant

__all__ = ["db"]

BASE = datetime.now(UTC) - timedelta(hours=1)


async def agents(db: DbConnection, count: int) -> list[UUID]:
    tenant_id = await tenant(db)
    creator = await member(db, tenant_id)
    return [await agent(db, tenant_id, creator) for _ in range(count)]


async def trigger(agent_id: UUID, *, enabled: bool = True) -> UUID:
    record = await agent_trigger_dao.create(
        obj_in={"agent_id": agent_id, "name": f"hook-{uuid4().hex[:8]}", "type": "webhook", "config": {}}
    )
    if not enabled:
        _ = await agent_trigger_dao.update(db_obj=record, obj_in={"is_enabled": False})
    return record.id


async def queue(
    db: DbConnection,
    trigger_id: UUID,
    agent_id: UUID,
    second: int,
    *,
    expired_owner: str | None = None,
) -> UUID:
    """A pending execution, or with ``expired_owner`` a started one whose lease has already expired."""
    execution_id = uuid4()
    expired = expired_owner is not None
    await db.execute(
        "INSERT INTO trigger_executions (id, trigger_id, agent_id, source, status, idempotency_key, payload, "
        "payload_text, lease_owner, lease_expires_at, scheduled_at, started_at) VALUES (%(id)s, %(trigger)s, "
        "%(agent)s, 'webhook', %(status)s, %(key)s, '{}'::jsonb, '', %(owner)s, %(expires)s, %(at)s, %(started)s)",
        {
            "id": execution_id,
            "trigger": trigger_id,
            "agent": agent_id,
            "status": "processing" if expired else "pending",
            "key": execution_id.hex,
            "owner": expired_owner,
            "expires": BASE if expired else None,
            "at": BASE + timedelta(seconds=second),
            "started": BASE if expired else None,
        },
    )
    return execution_id


async def owners(db: DbConnection) -> dict[object, tuple[object, object]]:
    rows = await db.fetchall("SELECT id, status, lease_owner FROM trigger_executions")
    return {row["id"]: (row["status"], row["lease_owner"]) for row in rows}


def leased(pairs: list[tuple[TriggerExecutionRecord, AgentTriggerRecord]]) -> tuple[set[UUID], set[str | None]]:
    return {execution.id for execution, _trigger in pairs}, {execution.lease_owner for execution, _trigger in pairs}


async def fire_count(trigger_id: UUID) -> int:
    record = await agent_trigger_dao.get(trigger_id)
    assert record is not None
    return record.fire_count


async def test_competing_one_slot_claims_each_batch_one_agent_and_leave_the_rest_unowned(
    db: DbConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given three agents: A with the oldest three rows, B with two, C with a pending row and an expired lease
    a, b, c = await agents(db, 3)
    ta, tb, tc = await trigger(a), await trigger(b), await trigger(c)
    a_rows = {await queue(db, ta, a, second) for second in (0, 1, 2)}
    b_rows = {await queue(db, tb, b, second) for second in (3, 4)}
    c_pending = await queue(db, tc, c, 5)
    c_expired = await queue(db, tc, c, 6, expired_owner="crashed-worker")
    locked, proceed = asyncio.Event(), asyncio.Event()
    select = trigger_execution_dao.claim_pending
    first_call = True

    async def hold_after_select(
        *, sources: list[str], now: datetime, max_agents: int, limit: int
    ) -> list[tuple[TriggerExecutionRecord, AgentTriggerRecord]]:
        nonlocal first_call
        rows = await select(sources=sources, now=now, max_agents=max_agents, limit=limit)
        if first_call:
            first_call = False
            locked.set()
            _ = await proceed.wait()
        return rows

    monkeypatch.setattr(trigger_execution_dao, "claim_pending", hold_after_select)
    # When one worker holds its claim transaction open while a second worker claims with one free slot
    first = asyncio.create_task(executions.claim_pending_trigger_executions(max_agents=1))
    await asyncio.wait_for(locked.wait(), 5)
    second = await asyncio.wait_for(executions.claim_pending_trigger_executions(max_agents=1), 5)
    proceed.set()
    first_pairs = await asyncio.wait_for(first, 5)
    # Then each worker leased every ready row of one agent, the second skipping the first one's locked rows
    first_ids, first_tokens = leased(first_pairs)
    second_ids, second_tokens = leased(second)
    assert first_ids == a_rows
    assert second_ids == b_rows
    assert len(first_tokens | second_tokens) == 2
    # And neither C's pending row nor its expired lease gained an owner, while first starts were counted per trigger
    stored = await owners(db)
    assert stored[c_pending] == ("pending", None)
    assert stored[c_expired] == ("processing", "crashed-worker")
    assert {stored[row] for row in a_rows} == {("processing", first_pairs[0][0].lease_owner)}
    assert [await fire_count(t) for t in (ta, tb, tc)] == [3, 2, 0]
    # And a later claim takes C's group whole, reclaiming the expired lease without counting it as a new start
    third_ids, _ = leased(await executions.claim_pending_trigger_executions(max_agents=5))
    assert third_ids == {c_pending, c_expired}
    assert await fire_count(tc) == 1


async def test_one_slot_batches_up_to_the_row_ceiling_and_never_starts_a_second_agent(db: DbConnection) -> None:
    # Given one agent with 101 ready rows and another agent with one
    a, b = await agents(db, 2)
    ta, tb = await trigger(a), await trigger(b)
    await db.execute(
        "INSERT INTO trigger_executions (id, trigger_id, agent_id, source, status, idempotency_key, payload, "
        "payload_text, scheduled_at) SELECT gen_random_uuid(), %(trigger)s, %(agent)s, 'webhook', 'pending', "
        "'k' || n, '{}'::jsonb, '', %(base)s + n * interval '1 second' FROM generate_series(1, 101) AS n",
        {"trigger": ta, "agent": a, "base": BASE},
    )
    b_row = await queue(db, tb, b, 50)
    # When a worker with one free slot claims, and later one with two
    first_ids, _ = leased(await executions.claim_pending_trigger_executions(max_agents=1))
    pending_a = await db.fetchall(
        "SELECT id FROM trigger_executions WHERE agent_id = %(agent)s AND status = 'pending'", {"agent": a}
    )
    second_ids, _ = leased(await executions.claim_pending_trigger_executions(max_agents=2))
    # Then the first claim took A's oldest 100 rows even though B's row is older than half of them
    assert len(first_ids) == 100
    assert b_row not in first_ids
    assert len(pending_a) == 1
    # And the second took A's last row and B's row as two groups
    assert second_ids == {pending_a[0]["id"], b_row}
    assert [await fire_count(t) for t in (ta, tb)] == [101, 1]


async def test_claim_skips_an_agent_whose_trigger_row_is_locked_instead_of_waiting(db: DbConnection) -> None:
    # Given A's older work behind a trigger row another transaction holds (as a trigger delete does)
    a, b = await agents(db, 2)
    ta, tb = await trigger(a), await trigger(b)
    a_row = await queue(db, ta, a, 0)
    b_row = await queue(db, tb, b, 1)
    async with db.raw.transaction():
        await db.execute("SELECT id FROM agent_triggers WHERE id = %(id)s FOR UPDATE", {"id": ta})
        # When a worker with one free slot claims
        claimed_ids, _ = leased(await asyncio.wait_for(executions.claim_pending_trigger_executions(max_agents=1), 5))
    # Then it leased B's work at once and left A's row unowned
    assert claimed_ids == {b_row}
    assert (await owners(db))[a_row] == ("pending", None)


async def test_saturated_dispatch_recovers_disabled_work_and_leases_nothing(db: DbConnection) -> None:
    # Given an expired lease of an externally disabled trigger, and ready work of an enabled one
    a, b = await agents(db, 2)
    disabled, enabled = await trigger(a, enabled=False), await trigger(b)
    abandoned = await queue(db, disabled, a, 0, expired_owner="crashed-worker")
    ready = await queue(db, enabled, b, 1)
    expired = await queue(db, enabled, b, 2, expired_owner="crashed-worker")
    # When a full process runs its claim phase with no free slot
    grouped, forced = await dispatch.claim_ready_trigger_invocations(datetime.now(UTC), max_agents=0)
    # Then the abandoned execution failed, and no ready row or expired lease of the enabled trigger changed owner
    assert (grouped, forced) == ({}, set())
    stored = await owners(db)
    assert stored[abandoned] == ("failed", None)
    assert stored[ready] == ("pending", None)
    assert stored[expired] == ("processing", "crashed-worker")


async def explain_claim_head(db: DbConnection, monkeypatch: pytest.MonkeyPatch, *, enable_seqscan: bool) -> str:
    """Run one claim and EXPLAIN the head statement it sent, with its parameters inlined as literals."""
    sent: list[tuple[str, Mapping[str, object]]] = []
    real_fetchone = DbConnection.fetchone

    async def recording_fetchone(self: DbConnection, query: str, params: Params = None) -> Row | None:
        if "LIMIT 1" in query and "FOR NO KEY UPDATE OF t SKIP LOCKED" in query and isinstance(params, Mapping):
            sent.append((query, params))
        return await real_fetchone(self, query, params)

    monkeypatch.setattr(DbConnection, "fetchone", recording_fetchone)
    _ = await trigger_execution_dao.claim_pending(
        sources=["webhook", "cron"], now=datetime.now(UTC), max_agents=1, limit=100
    )
    monkeypatch.undo()
    [(query, params)] = sent
    inlined = re.sub(r"%\((\w+)\)s", lambda match: sql.Literal(params[match[1]]).as_string(), query)
    await db.execute(f"SET enable_seqscan = {'on' if enable_seqscan else 'off'}")
    rows = await db.fetchall(f"EXPLAIN {inlined}")
    return "\n".join(str(next(iter(row.values()))) for row in rows)


async def test_pending_claim_is_served_by_the_partial_pending_index(
    db: DbConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given thousands of finished executions around a few pending ones, with fresh planner statistics
    (a,) = await agents(db, 1)
    ta = await trigger(a)
    await db.execute(
        "INSERT INTO trigger_executions (id, trigger_id, agent_id, source, status, idempotency_key, payload, "
        "payload_text, scheduled_at, finished_at) SELECT gen_random_uuid(), %(trigger)s, %(agent)s, 'webhook', "
        "'completed', 'done' || n, '{}'::jsonb, '', %(base)s + n * interval '1 second', %(base)s "
        "FROM generate_series(1, 5000) AS n",
        {"trigger": ta, "agent": a, "base": BASE},
    )
    _ = [await queue(db, ta, a, 10_000 + second) for second in range(3)]
    await db.execute("ANALYZE trigger_executions")
    # When the claim's head statement is planned, with sequential scans allowed as in production
    plan = await explain_claim_head(db, monkeypatch, enable_seqscan=True)
    # Then the pending work is read through the partial index and not by a scan of every execution
    assert "ix_trigger_executions_claimable" in plan, plan
    assert "Seq Scan on trigger_executions" not in plan, plan


async def seed_queue(db: DbConnection, trigger_id: UUID, agent_id: UUID) -> tuple[UUID, UUID, UUID]:
    """A pending row due before the disable, a pending row due later, and a started row with a live lease."""
    pending = await queue(db, trigger_id, agent_id, 0)
    later = await queue(db, trigger_id, agent_id, 1)
    running = await queue(db, trigger_id, agent_id, 2, expired_owner="live-worker")
    await db.execute(
        "UPDATE trigger_executions SET lease_expires_at = %(until)s WHERE id = %(id)s",
        {"id": running, "until": datetime.now(UTC) + timedelta(hours=1)},
    )
    return pending, later, running


async def assert_settled(db: DbConnection, settled: tuple[UUID, ...], untouched: dict[UUID, tuple[object, object]]):
    """The settled rows failed with the recorded reason; every other row kept its status and owner."""
    stored = await owners(db)
    for execution_id in settled:
        assert stored[execution_id] == ("failed", None)
        row = await db.fetchone(
            "SELECT last_error, finished_at FROM trigger_executions WHERE id = %(id)s", {"id": execution_id}
        )
        assert row is not None
        assert row["last_error"] == TRIGGER_DISABLED_ERROR
        assert row["finished_at"] is not None
    for execution_id, state in untouched.items():
        assert stored[execution_id] == state


async def test_disable_settles_pending_executions_and_enabling_again_never_runs_them(db: DbConnection) -> None:
    # Given a trigger with two pending executions and a live lease, and a neighbour's pending execution
    a, b = await agents(db, 2)
    ta, tb = await trigger(a), await trigger(b)
    pending, later, running = await seed_queue(db, ta, a)
    neighbour = await queue(db, tb, b, 3)
    record = await agent_trigger_dao.get(ta)
    assert record is not None
    # When it is disabled and enabled again, and a worker then claims with room for every agent
    _ = await agent_trigger_dao.update(db_obj=record, obj_in={"is_enabled": False})
    await assert_settled(db, (pending, later), {running: ("processing", "live-worker"), neighbour: ("pending", None)})
    reenabled = await agent_trigger_dao.get(ta)
    assert reenabled is not None
    _ = await agent_trigger_dao.update(db_obj=reenabled, obj_in={"is_enabled": True})
    claimed_ids, _tokens = leased(await executions.claim_pending_trigger_executions(max_agents=5))
    # Then only the neighbour's work ran: nothing that came due before the disable is leased again
    assert claimed_ids == {neighbour}
    assert (await owners(db))[pending][0] == "failed"


async def test_self_disable_and_plain_updates_keep_pending_executions(db: DbConnection) -> None:
    # Given a trigger with pending work
    (a,) = await agents(db, 1)
    ta = await trigger(a)
    pending, later, _running = await seed_queue(db, ta, a)
    record = await agent_trigger_dao.get(ta)
    assert record is not None
    # When it turns itself off (once or max_fires provenance) and is renamed
    record = await agent_trigger_dao.update(db_obj=record, obj_in={"is_enabled": False, "self_disabled": True})
    _ = await agent_trigger_dao.update(db_obj=record, obj_in={"name": "renamed"})
    # Then no pending execution was settled by those writes
    stored = await owners(db)
    assert [stored[row] for row in (pending, later)] == [("pending", None)] * 2


@pytest.mark.parametrize("scope", ["tenant", "creator", "creator_in_tenant", "agent"])
async def test_bulk_disables_settle_pending_executions_of_the_disabled_triggers(db: DbConnection, scope: str) -> None:
    # Given a disabled-to-be agent with pending work, and another tenant's agent with its own
    (a,) = await agents(db, 1)
    (b,) = await agents(db, 1)
    ta, tb = await trigger(a), await trigger(b)
    pending, later, running = await seed_queue(db, ta, a)
    neighbour = await queue(db, tb, b, 3)
    owner = await db.fetchone("SELECT tenant_id, creator_id FROM agents WHERE id = %(id)s", {"id": a})
    assert owner is not None
    tenant_id, creator_id = uuid_from_row(owner["tenant_id"]), uuid_from_row(owner["creator_id"])
    # When the tenant, the creator or the agent is disabled in bulk
    match scope:
        case "tenant":
            disabled = await agent_trigger_dao.disable_for_tenant(tenant_id)
        case "creator":
            disabled = await agent_trigger_dao.disable_for_creator(creator_id)
        case "creator_in_tenant":
            disabled = await agent_trigger_dao.disable_for_creator(creator_id, tenant_id=tenant_id)
        case _:
            disabled = await agent_trigger_dao.disable_for_agent(a)
    # Then only that trigger's pending executions are settled, with the recorded reason
    assert disabled == 1
    await assert_settled(db, (pending, later), {running: ("processing", "live-worker"), neighbour: ("pending", None)})


async def test_the_on_message_limiter_and_expiry_settle_pending_executions(
    db: DbConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given an on_message trigger at its hourly limit and an expired interval trigger, each with pending work
    a, b = await agents(db, 2)
    limited = await agent_trigger_dao.create(
        obj_in={"agent_id": a, "name": "limited", "type": "on_message", "config": {}}
    )
    expired = await agent_trigger_dao.create(
        obj_in={
            "agent_id": b,
            "name": "expired",
            "type": "interval",
            "config": {},
            "expires_at": datetime.now(UTC) - timedelta(minutes=1),
        }
    )
    limited_rows = await seed_queue(db, limited.id, a)
    expired_rows = await seed_queue(db, expired.id, b)
    now = datetime.now(UTC)
    monkeypatch.setattr(trigger_daemon, "_on_msg_fire_log", {a: [now] * trigger_daemon._ON_MSG_RATE_LIMIT})

    async def due(_trigger: AgentTriggerRecord, _now: datetime, _zone: str | None = None) -> bool:
        return True

    monkeypatch.setattr(trigger_daemon, "_evaluate_trigger", due)
    # When the daemon processes both
    await trigger_daemon._process_trigger(limited, now, None)
    await trigger_daemon._process_trigger(expired, now, None)
    # Then each auto-disabled trigger's pending executions are settled and its live lease is left alone
    for pending, later, running in (limited_rows, expired_rows):
        await assert_settled(db, (pending, later), {running: ("processing", "live-worker")})
    assert (await agent_trigger_dao.get(limited.id)) is not None
    assert not (await agent_trigger_dao.get(limited.id) or limited).is_enabled
    assert not (await agent_trigger_dao.get(expired.id) or expired).is_enabled


async def test_retry_later_waits_for_its_time_and_gives_up_after_the_window(db: DbConnection) -> None:
    # Given two started executions, one fresh and one whose first start is older than the retry window
    a, b = await agents(db, 2)
    ta, tb = await trigger(a), await trigger(b)
    fresh = await queue(db, ta, a, 0, expired_owner="worker")
    stale = await queue(db, tb, b, 1, expired_owner="worker")
    now = datetime.now(UTC)
    await db.execute(
        "UPDATE trigger_executions SET started_at = %(at)s WHERE id = %(id)s",
        {"at": now - executions.EXECUTION_RETRY_WINDOW - timedelta(minutes=1), "id": stale},
    )
    await db.execute("UPDATE trigger_executions SET lease_expires_at = %(at)s", {"at": now + timedelta(hours=1)})
    for execution_id in (fresh, stale):
        _ = await trigger_execution_dao.retry_later(
            execution_id,
            lease_token="worker",
            retry_at=now + executions.EXECUTION_RETRY_DELAY,
            give_up_before=now - executions.EXECUTION_RETRY_WINDOW,
            error_text="provider down",
            now=now,
        )
    # When a worker claims before the retry time, and again once it has passed
    early = await executions.claim_pending_trigger_executions(max_agents=5)
    await db.execute("UPDATE trigger_executions SET scheduled_at = %(at)s", {"at": now - timedelta(seconds=1)})
    late = await executions.claim_pending_trigger_executions(max_agents=5)
    # Then the fresh execution was queued again with its failure recorded and claimed only when due,
    # the stale one failed for good, and the first start was counted once
    assert early == []
    assert leased(late)[0] == {fresh}
    stored = await owners(db)
    assert stored[stale] == ("failed", None)
    row = await db.fetchone("SELECT last_error FROM trigger_executions WHERE id = %(id)s", {"id": stale})
    assert row is not None
    assert row["last_error"] == "provider down"
    assert [await fire_count(t) for t in (ta, tb)] == [0, 0]
