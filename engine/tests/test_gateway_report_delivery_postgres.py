import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import anyio
import pytest
from psycopg.types.json import Jsonb

from app.dao.gateway_report_delivery_dao import gateway_report_delivery_dao as dao
from app.db.errors import CheckViolationError
from app.db.session import transaction
from app.records.outbound_plan import OutboundProvider, OutboundTarget
from app.services.channels.outbound_plan import build_plan
from test_chat_submission_postgres import ReceiptDatabase, receipt_db

__all__ = ["receipt_db"]


@pytest.fixture
async def delivery_db(receipt_db: ReceiptDatabase):
    db = receipt_db
    message_id = uuid4()
    async with transaction(db.first):
        await db.first.execute(
            "INSERT INTO gateway_messages (id, agent_id, content, status, result) "
            "VALUES (%(id)s, %(agent)s, 'request', 'completed', 'result')",
            {"id": message_id, "agent": db.agent.id},
        )
        row = await dao.create_intent(
            message_id=message_id, destination_kind="im", destination_id=db.session_id, tenant_id=None
        )
    assert row is not None
    return db, row


def plan_for(row, content="a" * 4001):
    return build_plan(
        OutboundTarget(provider=OutboundProvider.SLACK, config_id=uuid4(), account=("account",), destination="C1"),
        content,
        row.id,
    )


async def claim(db):
    return (await dao.claim_batch(limit=20, lease_seconds=60, now=datetime.now(UTC)))[0]


async def test_real_claims_skip_locked_and_plan_initialization_is_null_only(delivery_db):
    db, row = delivery_db
    plan = plan_for(row)
    async with transaction(db.first):
        current = await claim(db)
        async with transaction(db.second):
            competing = await dao.claim_batch(limit=20, lease_seconds=60, now=datetime.now(UTC))
        initialized = await dao.initialize_plan(current, plan, now=datetime.now(UTC))
        replaced = await dao.initialize_plan(current, plan_for(row, "replacement"), now=datetime.now(UTC))

    async with transaction(db.second):
        stored = await dao.get(row.id)
    assert competing == []
    assert initialized
    assert not replaced
    assert stored is not None
    assert stored.im_plan == plan
    assert stored.next_chunk == 0


async def test_real_cursor_progress_releases_claim_and_final_ack_is_atomic(delivery_db):
    db, row = delivery_db
    plan = plan_for(row)
    async with transaction(db.first):
        first = await claim(db)
        assert await dao.initialize_plan(first, plan, now=datetime.now(UTC))
        assert not await dao.acknowledge(first, now=datetime.now(UTC))
        assert await dao.checkpoint_chunk(first, now=datetime.now(UTC))
    async with transaction(db.second):
        second = await claim(db)
        assert second.next_chunk == 1
        assert second.delivered_at is None
        assert not await dao.checkpoint_chunk(replace(second, next_chunk=0), now=datetime.now(UTC))
    async with transaction(db.first):
        assert await dao.checkpoint_chunk(second, now=datetime.now(UTC))
        async with transaction(db.second):
            before_commit = await dao.get(row.id)
        assert before_commit is not None
        assert before_commit.next_chunk == 1
        assert before_commit.delivered_at is None
    async with transaction(db.second):
        stored = await dao.get(row.id)
        more = await dao.claim_batch(limit=20, lease_seconds=60, now=datetime.now(UTC))
    assert stored is not None
    assert stored.next_chunk == 2
    assert stored.delivered_at is not None
    assert stored.claim_token is None
    assert stored.lease_expires_at is None
    assert more == []


async def test_real_stale_token_cannot_initialize_checkpoint_retry_or_finish(delivery_db):
    db, row = delivery_db
    plan = plan_for(row)
    async with transaction(db.first):
        old = await claim(db)
        await db.first.execute(
            "UPDATE gateway_report_deliveries SET lease_expires_at = clock_timestamp() "
            "- interval '1 second' WHERE id = %(id)s",
            {"id": row.id},
        )
    async with transaction(db.second):
        current = await claim(db)
    now = datetime.now(UTC)

    async with transaction(db.first):
        assert not await dao.initialize_plan(old, plan, now=now)
        assert not await dao.checkpoint_chunk(old, now=now)
        assert not await dao.acknowledge(old, now=now)
        assert not await dao.retry(old, available_at=now, now=now)
        assert old.claim_token is not None
        assert not await dao.finish_terminal(old.id, old.claim_token, "payload_rejected", now=now)
    async with transaction(db.second):
        assert await dao.initialize_plan(current, plan, now=datetime.now(UTC))
        assert await dao.checkpoint_chunk(current, now=datetime.now(UTC))


async def test_real_database_clock_rejects_expiry_even_with_stale_application_time(delivery_db):
    db, row = delivery_db
    async with transaction(db.first):
        current = await claim(db)
        assert await dao.initialize_plan(current, plan_for(row), now=datetime.now(UTC))
        earlier = datetime.now(UTC) - timedelta(seconds=10)
        await db.first.execute(
            "UPDATE gateway_report_deliveries SET lease_expires_at = %(expiry)s WHERE id = %(id)s",
            {"id": row.id, "expiry": earlier + timedelta(seconds=5)},
        )

    async with transaction(db.second):
        advanced = await dao.checkpoint_chunk(current, now=earlier)

    assert not advanced


async def test_real_final_checkpoint_rolls_back_cursor_and_ack_together(delivery_db):
    db, row = delivery_db
    async with transaction(db.first):
        current = await claim(db)
        assert await dao.initialize_plan(current, plan_for(row, "one chunk"), now=datetime.now(UTC))

    async def interrupted_checkpoint():
        async with transaction(db.first):
            assert await dao.checkpoint_chunk(current, now=datetime.now(UTC))
            raise RuntimeError("interrupt commit")

    with pytest.raises(RuntimeError, match="interrupt commit"):
        await interrupted_checkpoint()

    async with transaction(db.second):
        stored = await dao.get(row.id)
    assert stored is not None
    assert stored.next_chunk == 0
    assert stored.delivered_at is None
    assert stored.claim_token == current.claim_token


@pytest.mark.parametrize("change", ["null_plan_cursor", "negative_cursor", "past_end", "invalid_version"])
async def test_real_schema_rejects_invalid_plan_cursor_states(delivery_db, change):
    db, row = delivery_db
    plan = plan_for(row).model_dump(mode="json")
    cursor = 0
    match change:
        case "null_plan_cursor":
            plan, cursor = None, 1
        case "negative_cursor":
            cursor = -1
        case "past_end":
            cursor = 3
        case "invalid_version":
            plan["version"] = 3

    with pytest.raises(CheckViolationError):
        async with transaction(db.first):
            await db.first.execute(
                "UPDATE gateway_report_deliveries SET im_plan = %(plan)s, next_chunk = %(cursor)s WHERE id = %(id)s",
                {"id": row.id, "plan": Jsonb(plan) if plan else None, "cursor": cursor},
            )


BIG_BACKLOG = 12
SMALL_BACKLOG = 3
TENANT_CAP = 4


@pytest.fixture
async def tenant_backlog(receipt_db: ReceiptDatabase) -> tuple[ReceiptDatabase, UUID, list[UUID], UUID, list[UUID]]:
    db = receipt_db
    big, small = uuid4(), uuid4()
    due_since = datetime.now(UTC) - timedelta(hours=1)
    ids: dict[UUID, list[UUID]] = {big: [], small: []}
    async with transaction(db.first):
        for tenant, name in ((big, "big"), (small, "small")):
            await db.first.execute(
                "INSERT INTO tenants (id, name, slug, tier) VALUES (%(id)s, %(name)s, %(name)s, 'ultra')",
                {"id": tenant, "name": name},
            )
        for tenant, count in ((big, BIG_BACKLOG), (small, SMALL_BACKLOG)):
            for position in range(count):
                message_id = uuid4()
                await db.first.execute(
                    "INSERT INTO gateway_messages (id, agent_id, content, status, result) "
                    "VALUES (%(id)s, %(agent)s, 'request', 'completed', 'result')",
                    {"id": message_id, "agent": db.agent.id},
                )
                created = await dao.create_intent(
                    message_id=message_id, destination_kind="im", destination_id=db.session_id, tenant_id=tenant
                )
                assert created is not None
                await db.first.execute(
                    "UPDATE gateway_report_deliveries SET available_at = %(at)s WHERE id = %(id)s",
                    {"id": created.id, "at": due_since + timedelta(seconds=position)},
                )
                ids[tenant].append(created.id)
    return db, big, ids[big], small, ids[small]


async def test_two_relays_and_two_tenants_cap_the_noisy_tenant_without_blocking_or_double_claims(tenant_backlog):
    db, big, big_ids, small, small_ids = tenant_backlog
    now = datetime.now(UTC)

    async with transaction(db.first):
        first = await dao.claim_batch(limit=20, lease_seconds=60, now=now, per_tenant_limit=TENANT_CAP)
        async with transaction(db.second):
            with anyio.fail_after(5):
                concurrent = await dao.claim_batch(limit=20, lease_seconds=60, now=now, per_tenant_limit=TENANT_CAP)

    assert [r.id for r in first if r.tenant_id == big] == big_ids[:TENANT_CAP]
    assert [r.id for r in first if r.tenant_id == small] == small_ids
    assert [r.tenant_id for r in first[:2]] == [big, small] or [r.tenant_id for r in first[:2]] == [small, big]
    assert concurrent == []

    async with transaction(db.second):
        second = await dao.claim_batch(limit=20, lease_seconds=60, now=now, per_tenant_limit=TENANT_CAP)
    async with transaction(db.first):
        third = await dao.claim_batch(limit=20, lease_seconds=60, now=now, per_tenant_limit=TENANT_CAP)
        drained = await dao.claim_batch(limit=20, lease_seconds=60, now=now, per_tenant_limit=TENANT_CAP)

    assert [r.id for r in second] == big_ids[TENANT_CAP : 2 * TENANT_CAP]
    assert [r.id for r in third] == big_ids[2 * TENANT_CAP :]
    assert drained == []
    claimed = [r.id for r in (*first, *second, *third)]
    assert len(claimed) == len(set(claimed)) == BIG_BACKLOG + SMALL_BACKLOG


async def test_racing_relays_never_claim_one_row_twice_or_exceed_the_tenant_cap(tenant_backlog):
    db, big, _big_ids, small, _small_ids = tenant_backlog
    now = datetime.now(UTC)

    async def relay_claim(connection):
        async with transaction(connection):
            return await dao.claim_batch(limit=20, lease_seconds=60, now=now, per_tenant_limit=TENANT_CAP)

    first, second = await asyncio.gather(relay_claim(db.first), relay_claim(db.second))

    claimed = [r.id for r in (*first, *second)]
    assert len(claimed) == len(set(claimed))
    for batch in (first, second):
        for tenant in (big, small):
            assert sum(r.tenant_id == tenant for r in batch) <= TENANT_CAP


async def test_failed_delivery_keeps_its_reason_and_only_a_finished_one_is_purgeable(delivery_db):
    db, row = delivery_db
    horizon = datetime.now(UTC) + timedelta(hours=1)
    async with transaction(db.first):
        current = await claim(db)
        assert await dao.purge_finished_before(finished_before=horizon, limit=10) == 0
        assert current.claim_token is not None
        assert await dao.finish_terminal(
            current.id,
            current.claim_token,
            "provider_permanent_error",
            now=datetime.now(UTC),
            detail="slack: channel_not_found",
        )
    async with transaction(db.second):
        stored = await dao.get(row.id)
        assert stored is not None
        assert (stored.terminal_reason, stored.failure_detail) == (
            "provider_permanent_error",
            "slack: channel_not_found",
        )
        assert stored.terminal_at is not None
        assert stored.created_at is not None
        assert await dao.claim_batch(limit=20, lease_seconds=60, now=horizon) == []
        assert await dao.purge_finished_before(finished_before=horizon, limit=10) == 1


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE gateway_report_deliveries SET failure_detail = 'x' WHERE id = %(id)s",
        (
            "UPDATE gateway_report_deliveries SET terminal_reason = 'age_exhausted', terminal_at = now(), "
            "failure_detail = repeat('x', 201) WHERE id = %(id)s"
        ),
    ],
    ids=["pending_with_detail", "oversized_detail"],
)
async def test_real_schema_rejects_failure_detail_outside_a_bounded_terminal_row(delivery_db, statement):
    db, row = delivery_db
    with pytest.raises(CheckViolationError):
        await update_in_transaction(db, statement, row.id)


async def update_in_transaction(db, statement, row_id):
    async with transaction(db.first):
        await db.first.execute(statement, {"id": row_id})
