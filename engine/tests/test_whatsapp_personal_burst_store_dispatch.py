from uuid import uuid4

import pytest
from tests.test_chat_submission_postgres import receipt_db
from tests.test_whatsapp_personal_burst_store_fixtures import (
    BurstDatabase,
    admit,
    age,
    anchor,
    batch_of,
    burst_db,
    members_of,
    present,
    receive,
)

from app.dao.gateway_message_dao import gateway_message_dao
from app.dao.personal_burst_dao import personal_burst_dao
from app.dao.personal_burst_dispatch_dao import personal_burst_dispatch_dao as dispatch
from app.db.errors import CheckViolationError
from app.db.session import transaction
from app.records.personal_burst import BURST_CANCELLED_RESULT
from app.records.personal_burst_errors import PersonalBurstAnchorError

__all__ = ["burst_db", "receipt_db"]


async def intent(f: BurstDatabase, gateway_id) -> None:
    async with transaction(f.db.first):
        await f.db.first.execute(
            "INSERT INTO gateway_report_deliveries (id, message_id, destination_kind, destination_id) "
            "VALUES (%(id)s, %(m)s, 'im', %(d)s)",
            {"id": uuid4(), "m": gateway_id, "d": f.scope.reply_session_id},
        )


async def test_head_is_the_actual_oldest_pending_even_when_unready_and_never_filtered(burst_db):
    # Given an unready batch followed by an immediately ready legacy row.
    f = burst_db
    batch = await admit(f, "n1", "a")
    async with transaction(f.db.first):
        legacy = await anchor(f, content="legacy")
    # When the head is read, Then the unready batch is the head (not skipped for the ready legacy row).
    async with transaction(f.db.first):
        head = present(await dispatch.head(f.scope.agent_id, f.scope.shared_session_id))
    assert (head.gateway_id, head.due, head.active) == (batch.gateway_id, False, False)
    assert head.batch is not None
    # And once due it is claimable first; the legacy row retains immediate readiness behind it.
    await age(f, batch.gateway_id, 2.5)
    async with transaction(f.db.first):
        assert present(await dispatch.head(f.scope.agent_id, f.scope.shared_session_id)).due is True
        await f.db.first.execute("DELETE FROM gateway_messages WHERE id = %(id)s", {"id": batch.gateway_id})
        legacy_head = present(await dispatch.head(f.scope.agent_id, f.scope.shared_session_id))
    assert (legacy_head.gateway_id, legacy_head.batch, legacy_head.due) == (legacy, None, True)


async def test_head_reports_active_delivered_turn(burst_db):
    f = burst_db
    batch = await admit(f, "n1", "a")
    async with transaction(f.db.first):
        active = await anchor(f, content="active")
        await f.db.first.execute("UPDATE gateway_messages SET status = 'delivered' WHERE id = %(id)s", {"id": active})
        head = present(await dispatch.head(f.scope.agent_id, f.scope.shared_session_id))
    assert (head.gateway_id, head.active) == (batch.gateway_id, True)


async def test_seal_waits_for_db_time_then_freezes_exact_joined_body_and_blocks_appends(burst_db):
    f = burst_db
    batch = await admit(f, "n1", "first")
    await admit(f, "n2", "é\nsecond")
    # When not yet due, Then sealing leaves the batch collecting.
    async with transaction(f.db.first):
        assert present(await dispatch.seal(batch.gateway_id)).state == "pending"
    await age(f, batch.gateway_id, 2.5)
    async with transaction(f.db.first):
        sealed = present(await dispatch.seal(batch.gateway_id))
        again = present(await dispatch.seal(batch.gateway_id))
    # Then the body is the ordered originals joined by exactly two newlines, and sealing is idempotent.
    assert sealed.state == "sealed"
    assert sealed.frozen_body == "first\n\né\nsecond"
    assert sealed.body_bytes == len((sealed.frozen_body or "").encode())
    assert again.sealed_at == sealed.sealed_at
    # And new arrivals can no longer change it.
    later = await admit(f, "n3", "late")
    assert later.gateway_id != batch.gateway_id
    assert present(await batch_of(f, batch.gateway_id)).frozen_body == sealed.frozen_body


async def test_claimed_is_permanent_never_cancelled_or_requeued_and_completes_once(burst_db):
    f = burst_db
    batch = await admit(f, "n1", "a")
    await age(f, batch.gateway_id, 2.5)
    async with transaction(f.db.first):
        assert await dispatch.mark_claimed(batch.gateway_id) is False  # not sealed yet
        await dispatch.seal(batch.gateway_id)
        assert await dispatch.mark_claimed(batch.gateway_id) is True
        assert await dispatch.mark_claimed(batch.gateway_id) is False
        assert await dispatch.cancel_batch(batch.gateway_id, "disconnected") is None
        assert await dispatch.mark_completed(batch.gateway_id) is True
        assert await dispatch.mark_completed(batch.gateway_id) is False
    stored = present(await batch_of(f, batch.gateway_id))
    assert (stored.state, stored.cancel_reason) == ("completed", None)
    for state in ("pending", "sealed", "claimed", "cancelled"):
        with pytest.raises(CheckViolationError):
            async with transaction(f.db.first):
                await f.db.first.execute(
                    "UPDATE personal_burst_batches SET state = %(s)s WHERE gateway_id = %(id)s",
                    {"s": state, "id": batch.gateway_id},
                )


async def test_cancel_preserves_originals_quota_and_transcript_and_removes_queue_and_intent(burst_db):
    f = burst_db
    batch = await admit(f, "n1", "a")
    await admit(f, "n2", "b")
    await intent(f, batch.gateway_id)
    async with transaction(f.db.first):
        before = await f.db.first.fetchone(
            "SELECT quota_messages_used AS q FROM users WHERE id = %(u)s", {"u": f.db.user.id}
        )
        cancelled = await dispatch.cancel_batch(batch.gateway_id, "route_stale")
        assert await dispatch.cancel_batch(batch.gateway_id, "route_stale") is None
    assert cancelled is not None
    assert (cancelled.state, cancelled.cancel_reason) == ("cancelled", "route_stale")
    async with transaction(f.db.first):
        gateway = await f.db.first.fetchone(
            "SELECT status, conversation_id, result FROM gateway_messages WHERE id = %(id)s", {"id": batch.gateway_id}
        )
        left = await f.db.first.fetchone(
            "SELECT (SELECT count(*) FROM gateway_report_deliveries) AS intents, "
            "(SELECT count(*) FROM chat_messages WHERE conversation_id = %(c)s) AS transcript, "
            "(SELECT quota_messages_used FROM users WHERE id = %(u)s) AS quota",
            {"c": str(f.scope.shared_session_id), "u": f.db.user.id},
        )
        head = await dispatch.head(f.scope.agent_id, f.scope.shared_session_id)
    # Then the row leaves the FIFO and chat history, the undelivered intent is gone, nothing is refunded.
    assert gateway == {"status": "completed", "conversation_id": None, "result": BURST_CANCELLED_RESULT}
    assert left == {"intents": 0, "transcript": 2, "quota": before["q"]}
    assert head is None
    assert [m.text for m in await members_of(f, batch.gateway_id)] == ["a", "b"]
    # And cancellation is permanent: a cancelled batch can never be revived.
    with pytest.raises(CheckViolationError):
        async with transaction(f.db.first):
            await f.db.first.execute(
                "UPDATE personal_burst_batches SET state = 'pending' WHERE gateway_id = %(id)s",
                {"id": batch.gateway_id},
            )


async def test_cancel_open_filters_by_connection_and_covers_sealed_not_claimed(burst_db):
    f = burst_db
    first = await admit(f, "n1", "a")
    await age(f, first.gateway_id, 2.5)
    async with transaction(f.db.first):
        await dispatch.seal(first.gateway_id)
    second = await admit(f, "n2", "b")
    async with transaction(f.db.first):
        assert (
            await dispatch.cancel_open(f.scope.agent_id, f.scope.shared_session_id, "disconnected", config_id=uuid4())
            == []
        )
        opened = await dispatch.list_open(f.scope.agent_id, f.scope.shared_session_id)
        assert [b.gateway_id for b in opened] == [first.gateway_id, second.gateway_id]
        cancelled = await dispatch.cancel_open(f.scope.agent_id, f.scope.shared_session_id, "disconnected")
    assert cancelled == [first.gateway_id, second.gateway_id]


async def test_seal_cancels_batch_when_members_were_lost(burst_db):
    # Given the connector configuration (and its receipts) removed while the batch was collecting.
    f = burst_db
    batch = await admit(f, "n1", "a")
    await admit(f, "n2", "b")
    await age(f, batch.gateway_id, 2.5)
    async with transaction(f.db.first):
        await f.db.first.execute(
            "DELETE FROM channel_event_receipts WHERE config_id = %(c)s AND event_key = 'n2'", {"c": f.scope.config_id}
        )
        resolved = present(await dispatch.seal(batch.gateway_id))
    # Then the header survives as an explicit cancellation rather than dispatching a partial body.
    assert (resolved.state, resolved.cancel_reason) == ("cancelled", "members_lost")


async def test_sealed_batch_resumes_after_restart_from_durable_state(burst_db):
    # Given a sealed batch read by a different connection (a restarted worker).
    f = burst_db
    batch = await admit(f, "n1", "a")
    await age(f, batch.gateway_id, 2.5)
    async with transaction(f.db.first):
        await dispatch.seal(batch.gateway_id)
    async with transaction(f.db.second):
        heads, _ = await gateway_message_dao.pending_dispatch_page(5)
        sealed = present(await dispatch.get(batch.gateway_id))
    assert [head.id for head in heads] == [batch.gateway_id]
    assert (sealed.state, sealed.frozen_body) == ("sealed", "a")


async def test_equal_timestamps_keep_gateway_fifo_by_created_at_then_id(burst_db):
    f = burst_db
    async with transaction(f.db.first):
        same = await personal_burst_dao.next_created_at(f.scope.agent_id, f.scope.shared_session_id)
        older, newer = sorted([await anchor(f, at=same), await anchor(f, at=same)])
        member = await receive(f, "n1", "a")
        batch = await personal_burst_dao.create_batch(newer, f.scope, member, f.limits)
        tail = present(await personal_burst_dao.pending_tail(f.scope.agent_id, f.scope.shared_session_id))
        head = present(await dispatch.head(f.scope.agent_id, f.scope.shared_session_id))
    assert (tail.gateway_id, present(tail.batch).gateway_id) == (newer, batch.gateway_id)
    assert head.gateway_id == older
    # And an anchor that is not the (created_at, id) tail cannot be batched.
    async with transaction(f.db.first):
        late = await receive(f, "n2", "b")
        with pytest.raises(PersonalBurstAnchorError):
            await personal_burst_dao.create_batch(older, f.scope, late, f.limits)
