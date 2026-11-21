from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import anyio
import pytest

import test_shared_gateway_postgres as shared_fixtures
from app.dao.gateway_message_dao import gateway_message_dao
from app.db.connection import DbConnection
from app.db.errors import LockNotAvailableError
from app.db.session import get_connection, transaction
from app.records.agent import AgentRecord
from app.services import openclaw_gateway_delivery as delivery
from app.services.openclaw_shared_dispatch import claim_ready_shared
from test_chat_submission_postgres import ReceiptDatabase

receipt_database = shared_fixtures.receipt_database
pooled_database = shared_fixtures.pooled_database


async def test_shared_claim_and_native_settlement_finish_in_lock_order(
    pooled_database: ReceiptDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = pooled_database
    messages = await shared_fixtures.seed_human_lane(db)
    claim_holds_lane, settlement_attempts_agent, release_claim = anyio.Event(), anyio.Event(), anyio.Event()
    original_claim = gateway_message_dao.claim_head
    original_lock_agent = delivery.lock_agent
    claimed = []
    reports = []

    async def gated_claim(claim):
        connection = get_connection()
        assert connection is not None
        await connection.execute("SELECT id FROM agents WHERE id = %(id)s FOR SHARE", {"id": db.agent.id})
        claim_holds_lane.set()
        await release_claim.wait()
        return await original_claim(claim)

    async def observed_lock_agent(connection, agent_id, tenant_id):
        settlement_attempts_agent.set()
        return await original_lock_agent(connection, agent_id, tenant_id)

    monkeypatch.setattr(gateway_message_dao, "claim_head", gated_claim)
    monkeypatch.setattr(delivery, "lock_agent", observed_lock_agent)

    async def claim() -> None:
        claimed.append(await claim_ready_shared(shared_fixtures.human_request(messages)))

    async def settle() -> None:
        reports.append(await delivery.settle_native_report(db.agent, messages[0].id, "answer"))

    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(claim)
            await claim_holds_lane.wait()
            with pytest.raises(LockNotAvailableError):
                async with transaction(db.second):
                    await db.second.execute(
                        "SELECT id FROM agents WHERE id = %(id)s FOR UPDATE NOWAIT", {"id": db.agent.id}
                    )
            tasks.start_soon(settle)
            await settlement_attempts_agent.wait()
            release_claim.set()

    assert len(claimed) == len(reports) == 1
    assert claimed[0] is not None
    assert claimed[0].id == messages[0].id
    assert reports[0] is not None
    async with transaction(db.second):
        stored = await gateway_message_dao.get_for_agent(messages[0].id, db.agent.id)
    assert stored is not None
    assert (stored.status, stored.result) == ("completed", "answer")


async def test_ordinary_claim_race_keeps_uncertain_head_and_blocks_recovery(
    pooled_database: ReceiptDatabase,
) -> None:
    db = pooled_database
    at = datetime(2026, 10, 20, tzinfo=UTC)
    async with transaction(db.first):
        rows = [
            await gateway_message_dao.create(
                obj_in={
                    "agent_id": db.agent.id,
                    "conversation_id": str(db.session_id),
                    "content": str(index),
                    "status": "pending",
                    "created_at": at + timedelta(seconds=index),
                }
            )
            for index in range(2)
        ]
    claimed: list[UUID] = []

    async def claim(connection: DbConnection, message_id: UUID) -> None:
        async with transaction(connection):
            await connection.execute("SELECT id FROM agents WHERE id = %(id)s FOR UPDATE", {"id": db.agent.id})
            result = await gateway_message_dao.claim_ordinary_head(message_id, db.agent.id)
            if result is not None:
                claimed.append(result.id)

    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(claim, db.first, rows[0].id)
            tasks.start_soon(claim, db.second, rows[0].id)
    assert claimed == [rows[0].id]
    async with transaction(db.first):
        await db.first.execute(
            "UPDATE gateway_messages SET delivered_at = %(old)s WHERE id = %(id)s",
            {"old": at, "id": rows[0].id},
        )
        await db.first.execute("SELECT id FROM agents WHERE id = %(id)s FOR UPDATE", {"id": db.agent.id})
        blocked = await gateway_message_dao.claim_ordinary_head(rows[1].id, db.agent.id)
        pending, _ = await gateway_message_dao.pending_dispatch_page(50)
    assert blocked is None
    assert [item.id for item in pending] == []


async def test_native_settlement_holds_agent_lock_before_report_and_blocks_competing_claim(
    pooled_database: ReceiptDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = pooled_database
    messages = await shared_fixtures.seed_human_lane(db)
    first = await claim_ready_shared(shared_fixtures.human_request(messages))
    assert first is not None
    assert first.id == messages[0].id
    entered, release = anyio.Event(), anyio.Event()
    original = delivery.record_report
    reports: list[delivery.RecordedReport | None] = []

    async def gated_report(agent: AgentRecord, message_id: UUID, result: str) -> delivery.RecordedReport | None:
        entered.set()
        await release.wait()
        return await original(agent, message_id, result)

    monkeypatch.setattr(delivery, "record_report", gated_report)

    async def settle() -> None:
        reports.append(await delivery.settle_native_report(db.agent, first.id, "first answer"))

    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(settle)
            await entered.wait()
            with pytest.raises(LockNotAvailableError):
                async with transaction(db.second):
                    await db.second.execute(
                        "SELECT id FROM agents WHERE id = %(id)s FOR UPDATE NOWAIT", {"id": db.agent.id}
                    )
            assert await claim_ready_shared(shared_fixtures.human_request(messages)) is None
            release.set()

    assert len(reports) == 1
    assert reports[0] is not None
    next_message = await claim_ready_shared(shared_fixtures.human_request(messages))
    assert next_message is not None
    assert next_message.id == messages[1].id


async def test_two_users_canonical_lanes_run_together_and_each_stays_fifo(pooled_database: ReceiptDatabase) -> None:
    # Given two users' canonical lanes of one agent, three pending turns each.
    db = pooled_database
    member = uuid4()
    async with transaction(db.first):
        await db.first.execute(
            "INSERT INTO users (id, display_name, tenant_id) VALUES (%(id)s, 'Member', %(tenant)s)",
            {"id": member, "tenant": db.agent.tenant_id},
        )
    owner_lane = await shared_fixtures.seed_human_lane(db)
    member_lane = await shared_fixtures.seed_human_lane(db, member)
    # When each lane is claimed before either settles.
    owner_head = await claim_ready_shared(shared_fixtures.human_request(owner_lane))
    member_head = await claim_ready_shared(shared_fixtures.human_request(member_lane))
    # Then both heads are delivered at once, each bound to its own user's session,
    assert owner_head is not None
    assert member_head is not None
    assert (owner_head.id, member_head.id) == (owner_lane[0].id, member_lane[0].id)
    async with transaction(db.second):
        bindings = await db.second.fetchall(
            "SELECT g.id, b.session_id, b.user_id FROM gateway_messages g "
            "JOIN openclaw_reply_bindings b ON b.id = g.reply_binding_id "
            "WHERE g.agent_id = %(agent)s AND g.status = 'delivered' ORDER BY b.user_id = %(owner)s DESC",
            {"agent": db.agent.id, "owner": db.user.id},
        )
    assert bindings == [
        {"id": owner_head.id, "session_id": shared_fixtures.lane_of(owner_lane), "user_id": db.user.id},
        {"id": member_head.id, "session_id": shared_fixtures.lane_of(member_lane), "user_id": member},
    ]
    # while neither lane runs its next turn until its own head settles.
    assert await claim_ready_shared(shared_fixtures.human_request(owner_lane)) is None
    assert await claim_ready_shared(shared_fixtures.human_request(member_lane)) is None
    assert await delivery.settle_native_report(db.agent, owner_head.id, "owner answer") is not None
    owner_next = await claim_ready_shared(shared_fixtures.human_request(owner_lane))
    assert owner_next is not None
    assert owner_next.id == owner_lane[1].id
    assert await claim_ready_shared(shared_fixtures.human_request(member_lane)) is None


async def test_native_settlement_refuses_result_after_owner_changes(pooled_database: ReceiptDatabase) -> None:
    db = pooled_database
    messages = await shared_fixtures.seed_human_lane(db)
    first = await claim_ready_shared(shared_fixtures.human_request(messages))
    assert first is not None
    assert first.id == messages[0].id
    new_owner = uuid4()
    async with transaction(db.first):
        await db.first.execute("INSERT INTO users (id, display_name) VALUES (%(id)s, 'new owner')", {"id": new_owner})
        await db.first.execute(
            "UPDATE agents SET creator_id = %(owner)s WHERE id = %(id)s", {"owner": new_owner, "id": db.agent.id}
        )

    result = await delivery.settle_native_report(db.agent, first.id, "stale answer")

    async with transaction(db.second):
        stored = await gateway_message_dao.get_for_agent(first.id, db.agent.id)
    assert result is None
    assert stored is not None
    assert stored.status == "delivered"
    assert stored.result is None
