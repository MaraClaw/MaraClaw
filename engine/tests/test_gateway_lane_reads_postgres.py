import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

import test_shared_gateway_postgres as shared_fixtures
from app.dao.gateway_message_dao import PendingDuplicateProbe, gateway_message_dao
from app.db.session import transaction
from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord
from app.records.openclaw_dispatch import PreparedOpenClawMessage
from app.services import openclaw_enqueue, openclaw_routing as routing
from test_chat_submission_postgres import ReceiptDatabase

receipt_database = shared_fixtures.receipt_database
pooled_database = shared_fixtures.pooled_database

AT = datetime(2026, 11, 1, tzinfo=UTC)
# The shared receipt fixture replaces the module attribute with a no-op; keep the real wake for the paging test.
schedule_inbox_wake = routing._schedule_inbox_wake


async def seed(
    db: ReceiptDatabase, *rows: tuple[str | None, str, int], content: str = "turn"
) -> list[GatewayMessageRecord]:
    """Insert ``(conversation, status, second offset)`` rows for the fixture agent, in the given order."""
    async with transaction(db.first):
        return [
            await gateway_message_dao.create(
                obj_in={
                    "agent_id": db.agent.id,
                    "sender_user_id": db.user.id,
                    "conversation_id": lane,
                    "content": content,
                    "status": status,
                    "created_at": AT + timedelta(seconds=offset),
                    "delivered_at": AT if status == "delivered" else None,
                }
            )
            for lane, status, offset in rows
        ]


async def all_heads(db: ReceiptDatabase, limit: int) -> list[UUID]:
    heads: list[UUID] = []
    after = None
    while True:
        page, after = await gateway_message_dao.lane_heads_page(db.agent.id, limit, after=after)
        heads.extend(row.id for row in page)
        if after is None:
            return heads


async def test_lane_heads_are_the_oldest_pending_row_of_each_lane_in_order(pooled_database: ReceiptDatabase) -> None:
    # Given lanes interleaved in time, two conversationless rows, an empty-id row, and a lane that has delivered work.
    db = pooled_database
    a1, b1, none1, a2, empty1, none2, busy, delivered = await seed(
        db,
        ("a", "pending", 1),
        ("b", "pending", 2),
        (None, "pending", 3),
        ("a", "pending", 4),
        ("", "pending", 5),
        (None, "pending", 6),
        ("busy", "pending", 8),
        ("busy", "delivered", 7),
    )
    # And a not-yet-ready Personal burst on the head of lane "b", which the head read must not honour.
    async with transaction(db.first):
        await db.first.execute(
            "INSERT INTO personal_burst_batches (gateway_id, agent_id, tenant_id, shared_session_id, reply_session_id, "
            "sender_user_id, config_id, provider, account_id, auth_epoch, generation, peer_id, first_at, last_at, "
            "ready_at, member_count, member_bytes) VALUES (%(gateway)s, %(agent)s, %(any)s, %(any)s, %(any)s, %(any)s, "
            "%(any)s, 'whatsapp', 'acct', %(any)s, %(any)s, 'peer', %(at)s, %(at)s, %(ready)s, 1, 1)",
            {"gateway": b1.id, "agent": db.agent.id, "any": uuid4(), "at": AT, "ready": AT + timedelta(days=3650)},
        )
    # When paging with every page size around the number of heads.
    expected = [a1.id, b1.id, none1.id, empty1.id, none2.id, busy.id]
    # Then each lane contributes only its oldest pending row, NULL and '' rows each form their own lane, and neither
    # the delivered row nor the lane's busy state hides the pending head.
    assert delivered.status == "delivered"
    assert a2.id not in expected
    for limit in (1, 2, 4, 50):
        assert await all_heads(db, limit) == expected


async def test_lane_head_follows_the_row_lane_and_only_returns_pending_rows(pooled_database: ReceiptDatabase) -> None:
    # Given a shared lane with two rows, and conversationless rows with NULL and '' ids.
    db = pooled_database
    oldest, newer, null_row, other_null, empty_row = await seed(
        db,
        ("lane", "pending", 1),
        ("lane", "pending", 2),
        (None, "pending", 3),
        (None, "pending", 4),
        ("", "pending", 5),
    )
    # When reading from the newer member of the lane, and from conversationless members.
    # Then the lane's oldest pending row is returned, and per-message lanes return only the member itself.
    head = await gateway_message_dao.lane_head(newer)
    assert head is not None
    assert head.id == oldest.id
    for member in (null_row, other_null, empty_row):
        found = await gateway_message_dao.lane_head(member)
        assert found is not None
        assert found.id == member.id
    # And a claimed oldest row stops being the head while the delivered row is not returned.
    async with transaction(db.first):
        await db.first.execute("UPDATE gateway_messages SET status = 'delivered' WHERE id = %(id)s", {"id": oldest.id})
    after_claim = await gateway_message_dao.lane_head(newer)
    assert after_claim is not None
    assert after_claim.id == newer.id
    async with transaction(db.first):
        await db.first.execute(
            "UPDATE gateway_messages SET status = 'completed' WHERE id = %(id)s", {"id": null_row.id}
        )
    assert await gateway_message_dao.lane_head(null_row) is None


async def test_earliest_pending_duplicate_is_the_oldest_match_and_sees_the_callers_transaction(
    pooled_database: ReceiptDatabase,
) -> None:
    # Given an uncommitted pending pair written on the caller's connection.
    db = pooled_database
    other_sender = uuid4()
    probe = PendingDuplicateProbe(db.agent.id, db.user.id, "thread", "hello")
    async with transaction(db.first):
        early, late = [
            await gateway_message_dao.create(
                obj_in={
                    "agent_id": db.agent.id,
                    "sender_user_id": db.user.id,
                    "conversation_id": "thread",
                    "content": "hello",
                    "status": "pending",
                    "created_at": AT + timedelta(seconds=offset),
                }
            )
            for offset in (1, 2)
        ]
        # When looking up on that connection, then the earliest match is found although nothing is committed.
        found = await gateway_message_dao.earliest_pending_duplicate(probe)
        assert found is not None
        assert found.id == early.id
        assert late.id != early.id
        # And another connection cannot see the uncommitted pair.
        async with transaction(db.second):
            other = await db.second.fetchval("SELECT count(*) FROM gateway_messages WHERE content = 'hello'")
        assert other == 0
    # Then differing sender, content, lane and status never match.
    for miss in (
        PendingDuplicateProbe(db.agent.id, other_sender, "thread", "hello"),
        PendingDuplicateProbe(db.agent.id, db.user.id, "thread", "other"),
        PendingDuplicateProbe(db.agent.id, db.user.id, "elsewhere", "hello"),
        PendingDuplicateProbe(db.agent.id, db.user.id, None, "hello"),
    ):
        assert await gateway_message_dao.earliest_pending_duplicate(miss) is None
    async with transaction(db.first):
        await db.first.execute("UPDATE gateway_messages SET status = 'delivered' WHERE id = %(id)s", {"id": early.id})
    survivor = await gateway_message_dao.earliest_pending_duplicate(probe)
    assert survivor is not None
    assert survivor.id == late.id


@pytest.mark.parametrize(("stored", "retry"), [(None, ""), ("", None)])
async def test_enqueue_treats_null_and_empty_conversations_as_one_duplicate_lane(
    pooled_database: ReceiptDatabase, monkeypatch: pytest.MonkeyPatch, stored: str | None, retry: str | None
) -> None:
    # Given real enqueue against PostgreSQL for one sender.
    db = pooled_database

    async def eligible(_agent_id: UUID) -> AgentRecord:
        return db.agent

    monkeypatch.setattr(openclaw_enqueue, "load_agent_for_new_work", eligible)
    prepared = PreparedOpenClawMessage(db.agent, "hello", "primary", None, None, "lane-qa")

    async def enqueue(conversation: str | None, *, deduplicate: bool = True) -> GatewayMessageRecord:
        async with transaction(db.first):
            return await openclaw_enqueue.insert_prepared_message(
                prepared,
                sender_user_id=db.user.id,
                sender_agent_id=None,
                conversation_id=conversation,
                deduplicate=deduplicate,
            )

    # When the same text arrives in one anonymous form, then the other, then a named lane, and once with dedupe off.
    first = await enqueue(stored)
    empty = await enqueue(retry)
    named = await enqueue("thread")
    named_again = await enqueue("thread")
    forced = await enqueue(stored, deduplicate=False)
    # Then NULL and '' collapse to the earliest row, a named lane dedupes separately, and disabling dedupe inserts.
    assert empty.id == first.id
    assert named_again.id == named.id
    assert len({first.id, named.id, forced.id}) == 3


async def test_wake_starts_lanes_beyond_a_page_of_running_lanes(
    pooled_database: ReceiptDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a running lane whose queued follow-up heads the first page, then two more lanes and a per-message lane.
    db = pooled_database
    running_next, two, three, loose = await seed(
        db, ("one", "pending", 1), ("two", "pending", 2), ("three", "pending", 3), (None, "pending", 4)
    )
    started: list[UUID] = []

    async def execute(_agent: AgentRecord, _text: str, message_id: UUID) -> None:
        started.append(message_id)

    routing.open_wake_admission()
    monkeypatch.setattr(routing, "_LANE_HEAD_PAGE", 1)
    monkeypatch.setattr(routing, "_run_inbox_wake", execute)
    running_lane = (db.agent.id, "one")
    routing._lane_running.add(running_lane)
    try:
        # When a wake pages the real lane heads one row at a time.
        schedule_inbox_wake(db.agent, "", running_next.id)
        while routing._wake_tasks:
            await asyncio.wait_for(asyncio.gather(*routing._wake_tasks), 5)
        # Then the running lane only records that it must run again, and every later lane still starts.
        assert sorted(started) == sorted([two.id, three.id, loose.id])
        assert running_lane in routing._lane_again
    finally:
        routing._lane_running.discard(running_lane)
        routing._lane_again.discard(running_lane)
        await routing.drain_wake_tasks(0)
        routing.open_wake_admission()


async def test_delivered_lane_index_serves_the_delivered_probe(pooled_database: ReceiptDatabase) -> None:
    # Given the baseline applied twice by the fixture, and the planner barred from sequential scans.
    db = pooled_database
    async with transaction(db.first):
        await db.first.execute("SET LOCAL enable_seqscan = off")
        rows = await db.first.fetchall(
            "EXPLAIN SELECT 1 FROM gateway_messages d WHERE d.agent_id = %(agent)s "
            "AND d.conversation_id = %(lane)s AND d.status = 'delivered'",
            {"agent": db.agent.id, "lane": "lane"},
        )
    # Then the NOT EXISTS probe shape is answered from the partial delivered index.
    assert any("ix_gateway_messages_delivered_lane" in str(next(iter(row.values()))) for row in rows)
