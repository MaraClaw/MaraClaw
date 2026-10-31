from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from tests.test_chat_submission_postgres import receipt_db
from tests.test_whatsapp_personal_burst_store_fixtures import BurstDatabase, burst_db, receive

from app.dao.personal_burst_dao import personal_burst_dao
from app.dao.personal_burst_input_dao import personal_burst_input_dao
from app.dao.personal_busy_notice_dao import personal_busy_notice_dao as notices
from app.db.errors import CheckViolationError
from app.db.session import transaction
from app.records.personal_burst_errors import PersonalBurstAnchorError, PersonalBurstTransactionRequiredError

__all__ = ["burst_db", "receipt_db"]
COOLDOWN = 30


async def static_source(f: BurstDatabase, *, content: str = "", conversation: str | None = None) -> UUID:
    gateway_id = uuid4()
    await f.db.first.execute(
        "INSERT INTO gateway_messages (id, agent_id, conversation_id, content, status, result, routing_reason, "
        "completed_at) VALUES (%(id)s, %(agent)s, %(conv)s, %(content)s, 'completed', 'not queued', "
        "'whatsapp_busy', now())",
        {"id": gateway_id, "agent": f.scope.agent_id, "conv": conversation, "content": content},
    )
    return gateway_id


async def test_notice_creation_and_cooldown_are_scoped_to_the_full_frozen_scope(burst_db):
    f = burst_db
    async with transaction(f.db.first):
        assert await notices.find_recent(f.scope, COOLDOWN) is None
        source = await static_source(f)
        notice = await notices.create_notice(f.scope, source, "capacity")
        found = await notices.find_recent(f.scope, COOLDOWN)
        assert found is not None
        assert (found.id, found.gateway_id, found.scope, found.reason) == (notice.id, source, f.scope, "capacity")
        # A different generation/peer/reply session is a different cooldown scope.
        for other in (
            replace(f.scope, generation=uuid4()),
            replace(f.scope, peer_id="other"),
            replace(f.scope, reply_session_id=uuid4()),
        ):
            assert await notices.find_recent(other, COOLDOWN) is None
        # Once the cooldown elapsed a new notice is permitted.
        await f.db.first.execute("UPDATE personal_busy_notices SET created_at = created_at - interval '31 seconds'")
        assert await notices.find_recent(f.scope, COOLDOWN) is None


async def test_notice_source_must_be_the_static_completed_null_conversation_machine_row(burst_db):
    f = burst_db
    async with transaction(f.db.first):
        for source in (
            await static_source(f, content="not empty"),
            await static_source(f, conversation=str(f.scope.shared_session_id)),
            uuid4(),
        ):
            with pytest.raises(PersonalBurstAnchorError):
                await notices.create_notice(f.scope, source, "quota")
        chat = uuid4()
        await f.db.first.execute(
            "INSERT INTO gateway_messages (id, agent_id, sender_user_id, content, status, routing_reason) "
            "VALUES (%(id)s, %(a)s, %(u)s, '', 'pending', 'whatsapp_busy')",
            {"id": chat, "a": f.scope.agent_id, "u": f.db.user.id},
        )
        with pytest.raises(PersonalBurstAnchorError):
            await notices.create_notice(f.scope, chat, "quota")
    with pytest.raises(PersonalBurstTransactionRequiredError):
        await notices.create_notice(f.scope, uuid4(), "quota")


async def test_rejection_links_to_its_notice_once_and_notice_vanishes_with_its_source(burst_db):
    f = burst_db
    async with transaction(f.db.first):
        source = await static_source(f)
        notice = await notices.create_notice(f.scope, source, "capacity")
        first = await receive(f, "r1", "never retained")
        second = await receive(f, "r2", "never retained")
        await personal_burst_input_dao.record_rejection(first.receipt, "capacity_rejected", notice_id=notice.id)
        await personal_burst_input_dao.record_rejection(second.receipt, "capacity_rejected")
        assert await personal_burst_input_dao.link_notice(second.receipt, notice.id) is True
        assert await personal_burst_input_dao.link_notice(second.receipt, notice.id) is False
        linked = await personal_burst_input_dao.find_outcome(second.receipt)
        assert linked is not None
        assert linked.notice_id == notice.id
        # Administrative deletion of the undelivered source removes the notice but keeps the rejection outcome.
        await f.db.first.execute("DELETE FROM gateway_messages WHERE id = %(id)s", {"id": source})
        kept = await personal_burst_input_dao.find_outcome(first.receipt)
    assert kept is not None
    assert (kept.outcome, kept.notice_id) == ("capacity_rejected", None)


async def test_accepted_outcomes_cannot_carry_a_notice(burst_db):
    f = burst_db
    async with transaction(f.db.first):
        member = await receive(f, "n1", "a")
        source = await static_source(f)
        notice = await notices.create_notice(f.scope, source, "capacity")
        gw = uuid4()
        await f.db.first.execute(
            "INSERT INTO gateway_messages (id, agent_id, sender_user_id, conversation_id, content, status) "
            "VALUES (%(g)s, %(a)s, %(u)s, %(c)s, '', 'pending')",
            {"g": gw, "a": f.scope.agent_id, "u": f.db.user.id, "c": str(f.scope.shared_session_id)},
        )
        await personal_burst_dao.create_batch(gw, f.scope, member, f.limits)
    with pytest.raises(CheckViolationError):
        async with transaction(f.db.first):
            await f.db.first.execute(
                "UPDATE personal_burst_inputs SET notice_id = %(n)s WHERE event_key = 'n1'", {"n": notice.id}
            )


async def test_baseline_declares_composite_receipt_authority_and_no_second_dedupe_table(burst_db):
    f = burst_db
    async with transaction(f.db.first):
        columns = await f.db.first.fetchall(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'personal_burst_inputs'"
        )
        fk = await f.db.first.fetchone(
            "SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint "
            "WHERE conrelid = 'personal_burst_inputs'::regclass AND contype = 'f' "
            "AND confrelid = 'channel_event_receipts'::regclass"
        )
        tables = await f.db.first.fetchall(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() "
            "AND table_name LIKE 'personal_%'"
        )
        batch_fks = await f.db.first.fetchall(
            "SELECT confrelid::regclass::text AS parent, confdeltype AS action FROM pg_constraint "
            "WHERE conrelid = 'personal_burst_batches'::regclass AND contype = 'f'"
        )
    names = {c["column_name"] for c in columns}
    assert "receipt_id" not in names
    assert {"config_id", "provider", "event_key", "id"} <= names
    assert fk is not None
    assert "(config_id, provider, event_key)" in fk["def"]
    assert {t["table_name"] for t in tables} >= {
        "personal_burst_batches",
        "personal_burst_inputs",
        "personal_busy_notices",
    }
    # The header may only be removed by its source gateway (or agent): never by session/receipt/config/user cascades.
    assert sorted((r["parent"], r["action"]) for r in batch_fks) == [("agents", "c"), ("gateway_messages", "c")]
