from uuid import uuid4

import anyio
from tests.test_chat_submission_postgres import receipt_db
from tests.test_whatsapp_personal_burst_store_fixtures import (
    admit,
    admit_in_transaction,
    batch_of,
    burst_db,
    members_of,
    receive,
    wait_until_second_blocked,
)

from app.dao.agent_dao import AGENT_DELETE_CLEANUP_SQL
from app.dao.personal_burst_dispatch_dao import personal_burst_dispatch_dao as dispatch
from app.dao.personal_burst_input_dao import personal_burst_input_dao
from app.dao.personal_busy_notice_dao import personal_busy_notice_dao
from app.dao.tenant_dao import TENANT_DELETE_CLEANUP_SQL
from app.db.session import transaction
from app.records.personal_burst import PersonalBurstBatch

__all__ = ["burst_db", "receipt_db"]


async def test_side_row_deletion_never_turns_a_batched_gateway_into_a_legacy_ready_row(burst_db):
    f = burst_db
    batch = await admit(f, "n1", "a")
    async with transaction(f.db.first):
        await f.db.first.execute(
            "DELETE FROM chat_messages WHERE conversation_id = %(c)s", {"c": str(f.scope.shared_session_id)}
        )
        await f.db.first.execute("DELETE FROM channel_configs WHERE id = %(c)s", {"c": f.scope.config_id})
        await f.db.first.execute("DELETE FROM chat_sessions WHERE id = %(r)s", {"r": f.scope.reply_session_id})
        head = await dispatch.head(f.scope.agent_id, f.scope.shared_session_id)
    # Then the header is retained and not due; the transcript link and receipt-bound member rows are gone.
    assert head is not None
    assert (head.gateway_id, head.batch is not None, head.due) == (batch.gateway_id, True, False)
    assert await members_of(f, batch.gateway_id) == []


async def test_transcript_deletion_keeps_the_accepted_original(burst_db):
    f = burst_db
    batch = await admit(f, "n1", "a")
    async with transaction(f.db.first):
        await f.db.first.execute(
            "DELETE FROM chat_messages WHERE conversation_id = %(c)s", {"c": str(f.scope.shared_session_id)}
        )
    [member] = await members_of(f, batch.gateway_id)
    assert (member.text, member.chat_message_id) == ("a", None)


async def test_existing_agent_deletion_order_removes_batches_without_foreign_key_errors(burst_db):
    f = burst_db
    batch = await admit(f, "n1", "a")
    async with transaction(f.db.first):
        rejected = await receive(f, "r1", "x")
        await personal_burst_input_dao.record_rejection(rejected.receipt, "quota_rejected")
    async with transaction(f.db.first):
        for statement in AGENT_DELETE_CLEANUP_SQL:
            await f.db.first.execute(statement, {"aid": f.scope.agent_id})
        left = await f.db.first.fetchone(
            "SELECT (SELECT count(*) FROM personal_burst_batches) AS batches, "
            "(SELECT count(*) FROM personal_burst_inputs) AS inputs"
        )
    assert left == {"batches": 0, "inputs": 0}
    assert await batch_of(f, batch.gateway_id) is None


async def test_cancel_waits_for_an_inflight_append_then_cancels_every_original(burst_db):
    # Given an append that holds the shared-session lock without having committed.
    f = burst_db
    batch = await admit(f, "n1", "a")
    locked, release = anyio.Event(), anyio.Event()
    cancelled: list[PersonalBurstBatch | None] = []

    async def holder():
        async with transaction(f.db.first):
            await admit_in_transaction(f, "n2", "b", f.limits)
            locked.set()
            await release.wait()

    async def canceller():
        await locked.wait()
        async with transaction(f.db.second):
            cancelled.append(await dispatch.cancel_batch(batch.gateway_id, "ownership_lost"))

    # When a lifecycle cancellation races it, Then it blocks in PostgreSQL and then sees both originals.
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(holder)
            tasks.start_soon(canceller)
            await locked.wait()
            await wait_until_second_blocked(f)
            release.set()
    assert cancelled[0] is not None
    assert (cancelled[0].state, cancelled[0].member_count) == ("cancelled", 2)
    assert [m.text for m in await members_of(f, batch.gateway_id)] == ["a", "b"]


async def test_existing_tenant_deletion_order_removes_batches_and_notices_without_foreign_key_errors(burst_db):
    f = burst_db
    await admit(f, "n1", "a")
    async with transaction(f.db.first):
        source = uuid4()
        await f.db.first.execute(
            "INSERT INTO gateway_messages (id, agent_id, content, status, result, routing_reason) "
            "VALUES (%(id)s, %(a)s, '', 'completed', 'not queued', 'whatsapp_busy')",
            {"id": source, "a": f.scope.agent_id},
        )
        await personal_busy_notice_dao.create_notice(f.scope, source, "capacity")
        await f.db.first.execute("UPDATE agents SET tenant_id = %(t)s", {"t": f.scope.tenant_id})
        await f.db.first.execute("UPDATE users SET tenant_id = %(t)s", {"t": f.scope.tenant_id})
        for statement in TENANT_DELETE_CLEANUP_SQL:
            await f.db.first.execute(statement, {"tid": f.scope.tenant_id})
        left = await f.db.first.fetchone(
            "SELECT (SELECT count(*) FROM personal_burst_batches) AS batches, "
            "(SELECT count(*) FROM personal_busy_notices) AS notices, (SELECT count(*) FROM personal_burst_inputs) AS inputs"
        )
    assert left == {"batches": 0, "notices": 0, "inputs": 0}
