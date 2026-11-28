import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from psycopg import sql

import test_shared_gateway_postgres as shared_fixtures
from app.core.json_types import JsonObject, is_json_object
from app.dao.gateway_message_dao import PendingDuplicateProbe, gateway_message_dao
from app.db.connection import DbConnection, Params, Row
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


async def dispatch_walk(limit: int) -> tuple[list[UUID], int]:
    """Walk ``pending_dispatch_page`` until it wraps; return the heads in order and the number of empty pages."""
    heads: list[UUID] = []
    empty_pages = 0
    after: tuple[datetime, UUID] | None = None
    while True:
        page, cursor = await gateway_message_dao.pending_dispatch_page(limit, after=after)
        heads.extend(row.id for row in page)
        if cursor is None:
            return heads, empty_pages
        assert after is None or cursor > after
        empty_pages += not page
        after = cursor


async def seed_lane(db: ReceiptDatabase, lane: str, first: int, last: int) -> None:
    """Insert pending rows of one lane at second offsets ``first`` through ``last``, then refresh planner stats."""
    async with transaction(db.first):
        await db.first.execute(
            "INSERT INTO gateway_messages (id, agent_id, sender_user_id, conversation_id, content, status, created_at) "
            "SELECT gen_random_uuid(), %(agent)s, %(user)s, %(lane)s, 'turn', 'pending', %(at)s + n * interval '1 second' "
            "FROM generate_series(%(first)s::int, %(last)s::int) AS n",
            {"agent": db.agent.id, "user": db.user.id, "lane": lane, "at": AT, "first": first, "last": last},
        )
        await db.first.execute("ANALYZE gateway_messages")


async def seed_bound(db: ReceiptDatabase, count: int, status: str) -> None:
    """Insert ``count`` rows, each bound to the reply binding of its own web session, then refresh planner stats."""
    async with transaction(db.first):
        await db.first.execute(
            "WITH sessions AS (INSERT INTO chat_sessions (id, agent_id, user_id, title) "
            "SELECT gen_random_uuid(), %(agent)s, %(user)s, 'web' FROM generate_series(1, %(count)s::int) RETURNING id), "
            "bindings AS (INSERT INTO openclaw_reply_bindings (id, session_id, agent_id, tenant_id, owner_id, user_id) "
            "SELECT gen_random_uuid(), s.id, a.id, a.tenant_id, a.creator_id, %(user)s FROM sessions s "
            "JOIN agents a ON a.id = %(agent)s RETURNING id, session_id) "
            "INSERT INTO gateway_messages (id, agent_id, sender_user_id, conversation_id, content, status, "
            "reply_binding_id, created_at, delivered_at) "
            "SELECT gen_random_uuid(), %(agent)s, %(user)s, b.session_id::text, 'turn', %(status)s, b.id, %(at)s, %(at)s "
            "FROM bindings b",
            {"agent": db.agent.id, "user": db.user.id, "count": count, "status": status, "at": AT},
        )
        await db.first.execute("ANALYZE gateway_messages, openclaw_reply_bindings, chat_sessions, agents")


async def hold_in_burst(db: ReceiptDatabase, gateway: UUID, *, ready: datetime, cancelled: bool = False) -> None:
    """Attach a Personal burst header to a pending row: pending until ``ready``, or already cancelled."""
    async with transaction(db.first):
        await db.first.execute(
            "INSERT INTO personal_burst_batches (gateway_id, agent_id, tenant_id, transcript_session_id, reply_session_id, "
            "sender_user_id, config_id, provider, account_id, auth_epoch, generation, peer_id, first_at, last_at, "
            "ready_at, member_count, member_bytes, state, cancel_reason, finished_at) VALUES (%(gateway)s, %(agent)s, "
            "%(any)s, %(any)s, %(any)s, %(any)s, %(any)s, 'whatsapp', 'acct', %(any)s, %(any)s, 'peer', %(at)s, %(at)s, "
            "%(ready)s, 1, 1, %(state)s, %(reason)s, %(finished)s)",
            {
                "gateway": gateway,
                "agent": db.agent.id,
                "any": uuid4(),
                "at": AT,
                "ready": ready,
                "state": "cancelled" if cancelled else "pending",
                "reason": "operator" if cancelled else None,
                "finished": AT if cancelled else None,
            },
        )


type Statement = tuple[str, Mapping[str, object]]
type Plan = Mapping[str, object]


async def dao_statements[T](call: Callable[[], Awaitable[T]]) -> tuple[T, list[Statement]]:
    """Run one DAO call; return its result with the exact SQL and named parameters of every read it sent."""
    sent: list[Statement] = []
    fetchone, fetchall = DbConnection.fetchone, DbConnection.fetchall

    def record(query: str, params: Params) -> None:
        assert isinstance(params, Mapping)
        sent.append((query, params))

    async def recorded_fetchone(self: DbConnection, query: str, params: Params = None) -> Row | None:
        record(query, params)
        return await fetchone(self, query, params)

    async def recorded_fetchall(self: DbConnection, query: str, params: Params = None) -> list[Row]:
        record(query, params)
        return await fetchall(self, query, params)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(DbConnection, "fetchone", recorded_fetchone)
        patch.setattr(DbConnection, "fetchall", recorded_fetchall)
        result = await call()
    return result, sent


async def generic_plan(db: ReceiptDatabase, statement: Statement) -> JsonObject:
    """EXPLAIN ANALYZE one DAO statement as the generic plan psycopg's prepared statement settles on."""
    query, params = statement
    names: list[str] = []

    def number(match: re.Match[str]) -> str:
        if match[1] not in names:
            names.append(match[1])
        return f"${names.index(match[1]) + 1}"

    prepared = re.sub(r"%\((\w+)\)s", number, query)
    arguments = sql.SQL(", ").join(sql.Literal(params[name]) for name in names).as_string()
    async with transaction(db.first):
        await db.first.execute("SET LOCAL plan_cache_mode = force_generic_plan")
        await db.first.execute(f"PREPARE dispatch_page AS {prepared}")
        rows = await db.first.fetchall(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) EXECUTE dispatch_page ({arguments})")
        await db.first.execute("DEALLOCATE dispatch_page")
    explained = next(iter(rows[0].values()))
    assert isinstance(explained, list)
    plan: object = explained[0]["Plan"]
    assert is_json_object(plan)
    return plan


def driving_scan(plan: Plan) -> Plan:
    """Follow each node's outer or subquery input down to the scan that drives the plan."""
    nodes = plan.get("Plans", [])
    assert isinstance(nodes, list)
    outer = [node for node in nodes if node["Parent Relationship"] in ("Outer", "Subquery")]
    return driving_scan(outer[0]) if outer else plan


def outline(plan: Plan, depth: int = 0) -> str:
    """The plan tree, one node per line, for assertion messages."""
    fields = ("Node Type", "Index Name", "Index Cond", "Filter", "Actual Rows", "Rows Removed by Filter")
    line = "  " * depth + ", ".join(f"{field}: {plan[field]}" for field in fields if field in plan)
    nodes = plan.get("Plans", [])
    assert isinstance(nodes, list)
    return "\n".join([line, *(outline(node, depth + 1) for node in nodes)])


def rows_read(scan: Plan) -> float:
    """Rows a scan fetched, whether it returned them or its filter removed them."""
    returned, removed = scan["Actual Rows"], scan.get("Rows Removed by Filter", 0)
    assert isinstance(returned, int | float)
    assert isinstance(removed, int | float)
    return returned + removed


def buffers(plan: Plan) -> int:
    hit, read = plan["Shared Hit Blocks"], plan["Shared Read Blocks"]
    assert isinstance(hit, int)
    assert isinstance(read, int)
    return hit + read


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
    await hold_in_burst(db, b1.id, ready=AT + timedelta(days=3650))
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
    prepared = PreparedOpenClawMessage(db.agent, "hello", "primary", None, "lane-qa")

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


async def test_pending_order_index_starts_the_dispatch_walk_at_its_cursor(pooled_database: ReceiptDatabase) -> None:
    # Given the dispatch keyset walk planned generically, as a server-side prepared statement may run it.
    db = pooled_database
    async with transaction(db.first):
        await db.first.execute("SET LOCAL plan_cache_mode = force_generic_plan")
        await db.first.execute("SET LOCAL enable_seqscan = off")
        await db.first.execute("SET LOCAL enable_sort = off")
        await db.first.execute(
            "PREPARE dispatch_walk (timestamptz, uuid) AS SELECT g.id FROM gateway_messages g "
            "WHERE g.status = 'pending' AND (g.created_at, g.id) > ($1, $2) ORDER BY g.created_at, g.id LIMIT 51"
        )
        rows = await db.first.fetchall(
            "EXPLAIN EXECUTE dispatch_walk ('2026-11-01T00:00:00Z', '00000000-0000-0000-0000-000000000000')"
        )
        await db.first.execute("DEALLOCATE dispatch_walk")
    plan = "\n".join(str(next(iter(row.values()))) for row in rows)
    # Then the walk starts at the cursor inside the partial pending index, with no sort of every pending row.
    assert "ix_gateway_messages_pending_order" in plan, plan
    assert "Index Cond" in plan, plan
    assert "Sort" not in plan, plan


async def test_pending_page_past_the_last_head_scans_a_bounded_stretch(pooled_database: ReceiptDatabase) -> None:
    # Given the last lane head, with 300 pending rows of its lane queued behind it.
    db = pooled_database
    [head] = await seed(db, ("deep", "pending", 0))
    await seed_lane(db, "deep", 1, 300)
    assert head.created_at is not None
    after = (head.created_at, head.id)

    async def page_plans() -> list[Plan]:
        (rows, cursor), statements = await dao_statements(
            lambda: gateway_message_dao.pending_dispatch_page(5, after=after)
        )
        # Then the page holds no head, yet its cursor moves past the 100 rows (20 per possible head) it scanned.
        assert rows == []
        assert cursor is not None
        assert cursor[0] == AT + timedelta(seconds=100)
        return [await generic_plan(db, statement) for statement in statements]

    small = await page_plans()
    # When ten times as many rows queue behind the head.
    await seed_lane(db, "deep", 301, 3000)
    large = await page_plans()
    # Then each generic plan of the page starts at the cursor inside the pending index and reads only the scanned
    # stretch, so the page reads the same buffers whatever queues behind it.
    for plan in small + large:
        scan = driving_scan(plan)
        assert scan["Index Name"] == "ix_gateway_messages_pending_order", outline(plan)
        assert "Index Cond" in scan, outline(plan)
        assert rows_read(scan) == 100, outline(plan)
    assert [buffers(plan) for plan in large] == [buffers(plan) for plan in small], [
        outline(plan) for plan in small + large
    ]


async def test_delivered_page_starts_at_its_cursor_as_reply_bindings_grow(pooled_database: ReceiptDatabase) -> None:
    # Given three bound in-flight turns among 500 completed turns that each kept their reply binding.
    db = pooled_database
    await seed_bound(db, 3, "delivered")
    await seed_bound(db, 500, "completed")

    async def page_plan() -> Plan:
        (rows, cursor), [statement] = await dao_statements(lambda: gateway_message_dao.delivered_dispatch_page(50))
        assert len(rows) == 3
        assert cursor is None
        return await generic_plan(db, statement)

    small = await page_plan()
    # When reply bindings grow tenfold.
    await seed_bound(db, 4500, "completed")
    large = await page_plan()
    # Then the generic plan starts at the cursor inside the bound delivered-row index, and reads the same buffers.
    for plan in (small, large):
        scan = driving_scan(plan)
        assert scan["Index Name"] == "ix_gateway_messages_delivered_order", outline(plan)
        assert "Index Cond" in scan, outline(plan)
    assert buffers(large) == buffers(small), [outline(small), outline(large)]


async def test_pending_pages_return_every_head_once_in_order(pooled_database: ReceiptDatabase) -> None:
    # Given NULL and '' conversations, a lane whose earlier row completed, a lane with delivered work, Personal bursts
    # that are held, due and cancelled, and a deep lane longer than two limit-1 scans, then two later heads.
    db = pooled_database
    a1, none1, a2, empty1, _done, after_done, _busy, _busy_next, held, due, cancelled, deep = await seed(
        db,
        ("a", "pending", 1),
        (None, "pending", 2),
        ("a", "pending", 3),
        ("", "pending", 4),
        ("done", "completed", 5),
        ("done", "pending", 6),
        ("busy", "delivered", 7),
        ("busy", "pending", 8),
        ("held", "pending", 9),
        ("due", "pending", 10),
        ("cancelled", "pending", 11),
        ("deep", "pending", 12),
    )
    await seed_lane(db, "deep", 13, 60)
    none2, z1 = await seed(db, (None, "pending", 100), ("z", "pending", 101))
    await hold_in_burst(db, held.id, ready=AT + timedelta(days=3650))
    await hold_in_burst(db, due.id, ready=AT)
    await hold_in_burst(db, cancelled.id, ready=AT + timedelta(days=3650), cancelled=True)
    expected = [a1.id, none1.id, empty1.id, after_done.id, due.id, cancelled.id, deep.id, none2.id, z1.id]
    # When walking the pending pages at page sizes below, around and above the number of heads.
    # Then every page size returns each head once in (created_at, id) order, and at limit 1 (20-row scans) the deep
    # lane's queued rows cost a page that holds no head but still advances the cursor instead of wrapping.
    for limit in (1, 2, 3, 50):
        heads, empty_pages = await dispatch_walk(limit)
        assert heads == expected, limit
        assert empty_pages == (1 if limit == 1 else 0), limit
    # And once lane a's head completes, the next walk returns the lane's next row in its place.
    async with transaction(db.first):
        await db.first.execute("UPDATE gateway_messages SET status = 'completed' WHERE id = %(id)s", {"id": a1.id})
    heads, _ = await dispatch_walk(1)
    assert heads == [none1.id, a2.id, *expected[2:]]


async def test_agent_listing_index_serves_newest_first_without_a_sort(pooled_database: ReceiptDatabase) -> None:
    # Given the planner barred from sequential scans and sorts.
    db = pooled_database
    async with transaction(db.first):
        await db.first.execute("SET LOCAL enable_seqscan = off")
        await db.first.execute("SET LOCAL enable_sort = off")
        rows = await db.first.fetchall(
            "EXPLAIN SELECT id FROM gateway_messages WHERE agent_id = %(agent)s "
            "ORDER BY created_at DESC NULLS LAST LIMIT 50",
            {"agent": db.agent.id},
        )
    plan = "\n".join(str(next(iter(row.values()))) for row in rows)
    # Then the newest-first listing of every status reads one agent's index range in order.
    assert "ix_gateway_messages_agent_created" in plan, plan
    assert "Sort" not in plan, plan
