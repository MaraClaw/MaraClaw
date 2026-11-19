"""Real PostgreSQL: trigger claims bounded by agent groups and rows under skip-locked execution and trigger locks.

Opt in with BOOTSTRAP_TEST_DATABASE_URL; each test gets a unique schema that is dropped afterwards.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.dao.trigger_dao import agent_trigger_dao, trigger_execution_dao
from app.db.connection import DbConnection
from app.records.trigger import AgentTriggerRecord, TriggerExecutionRecord
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
