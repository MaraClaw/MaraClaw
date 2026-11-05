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
from app.records.openclaw_dispatch import SharedDispatchRequest
from app.services import openclaw_gateway_delivery as delivery
from app.services.openclaw_shared_dispatch import claim_ready_shared
from test_chat_submission_postgres import ReceiptDatabase

receipt_database = shared_fixtures.receipt_database
pooled_database = shared_fixtures.pooled_database


async def test_shared_claim_and_native_settlement_finish_in_lock_order(
    pooled_database: ReceiptDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = pooled_database
    messages = await shared_fixtures.seed_shared(db)
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
        claimed.append(await claim_ready_shared(SharedDispatchRequest(db.agent.id, datetime.now(UTC))))

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
    messages = await shared_fixtures.seed_shared(db)
    first = await claim_ready_shared(SharedDispatchRequest(db.agent.id, datetime.now(UTC)))
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
            assert await claim_ready_shared(SharedDispatchRequest(db.agent.id, datetime.now(UTC))) is None
            release.set()

    assert len(reports) == 1
    assert reports[0] is not None
    next_message = await claim_ready_shared(SharedDispatchRequest(db.agent.id, datetime.now(UTC)))
    assert next_message is not None
    assert next_message.id == messages[1].id


async def test_native_settlement_refuses_result_after_owner_changes(pooled_database: ReceiptDatabase) -> None:
    db = pooled_database
    messages = await shared_fixtures.seed_shared(db)
    first = await claim_ready_shared(SharedDispatchRequest(db.agent.id, datetime.now(UTC)))
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
