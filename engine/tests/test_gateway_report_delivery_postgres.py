from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from app.dao.gateway_report_delivery_dao import gateway_report_delivery_dao as dao
from app.db.errors import CheckViolationError
from app.db.session import transaction
from app.records.outbound_plan import OutboundTarget
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
        OutboundTarget(provider="slack", config_id=uuid4(), account=("account",), destination="C1"), content, row.id
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
            plan["version"] = 2

    with pytest.raises(CheckViolationError):
        async with transaction(db.first):
            await db.first.execute(
                "UPDATE gateway_report_deliveries SET im_plan = %(plan)s, next_chunk = %(cursor)s WHERE id = %(id)s",
                {"id": row.id, "plan": Jsonb(plan) if plan else None, "cursor": cursor},
            )
