from datetime import UTC, datetime, timedelta
from uuid import uuid4

import anyio

import test_chat_submission_postgres as receipt_fixtures
from app.dao.chat_dao import chat_session_dao
from app.dao.gateway_message_dao import gateway_message_dao
from app.dao.gateway_report_delivery_dao import gateway_report_delivery_dao
from app.db.session import transaction
from app.records.gateway_message import GatewayMessageRecord
from app.services import openclaw_routing
from app.services.openclaw_gateway_delivery import claim_poll_batch, record_report
from app.services.openclaw_turns import completed_history
from app.services.shared_chat import shared_session_id
from test_chat_submission_postgres import ReceiptDatabase

receipt_database = receipt_fixtures.receipt_db


async def seed_shared(db: ReceiptDatabase) -> list[GatewayMessageRecord]:
    session_id = shared_session_id(db.agent.id)
    async with transaction(db.first):
        await chat_session_dao.create(
            obj_in={
                "id": session_id,
                "agent_id": db.agent.id,
                "user_id": db.user.id,
                "title": "Shared",
                "source_channel": "shared",
                "is_primary": True,
            }
        )
        return [
            await gateway_message_dao.create(
                obj_in={
                    "agent_id": db.agent.id,
                    "sender_user_id": db.user.id,
                    "conversation_id": str(session_id),
                    "content": str(index),
                    "status": "pending",
                    "created_at": datetime(2026, 10, 20, tzinfo=UTC) + timedelta(seconds=index),
                }
            )
            for index in range(3)
        ]


async def test_shared_poll_and_wake_claim_only_fifo_head(receipt_database: ReceiptDatabase) -> None:
    # Given two workers competing for three shared turns.
    db = receipt_database
    messages = await seed_shared(db)
    claimed: list[GatewayMessageRecord] = []

    async def poll() -> None:
        async with transaction(db.first):
            claimed.extend(await claim_poll_batch(db.agent.id))

    async def wake() -> None:
        async with transaction(db.second):
            row = await gateway_message_dao.claim_shared(
                db.agent.id, shared_session_id(db.agent.id), now=datetime.now(UTC)
            )
            if row:
                claimed.append(row)

    # When wake and poll run concurrently using independent real connections.
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(poll)
        tasks.start_soon(wake)

    # Then only the earliest turn is executable, even after the normal timeout.
    assert [row.id for row in claimed] == [messages[0].id]
    async with transaction(db.first):
        assert await claim_poll_batch(db.agent.id, now=datetime.now(UTC) + timedelta(days=1)) == []


async def test_reply_intent_waits_for_completion_and_equal_human_inputs_remain_distinct(
    receipt_database: ReceiptDatabase,
) -> None:
    # Given a shared transcript and an independent provider session.
    db = receipt_database
    await seed_shared(db)
    prepared = openclaw_routing.PreparedOpenClawMessage(db.agent, "same", "primary", None, None, "qa")
    async with transaction(db.first):
        provider = await chat_session_dao.create(
            obj_in={
                "id": uuid4(),
                "agent_id": db.agent.id,
                "user_id": db.user.id,
                "title": "Slack",
                "source_channel": "slack",
                "external_conv_id": "C-QA",
            }
        )
        # When separate human events carry equal content through web and IM.
        web = await openclaw_routing.enqueue_prepared_openclaw_message(
            prepared, sender_user_id=db.user.id, conversation_id=str(shared_session_id(db.agent.id))
        )
        im = await openclaw_routing.enqueue_prepared_openclaw_message(
            prepared, sender_user_id=db.user.id, conversation_id=web.conversation_id, reply_session_id=provider.id
        )
        claims = await gateway_report_delivery_dao.claim_batch(limit=10, lease_seconds=60, now=datetime.now(UTC))

    # Then the durable destination exists independently but cannot send pending output.
    assert web.id != im.id
    assert claims == []
    async with transaction(db.first):
        intent = await db.first.fetchone(
            "SELECT destination_id FROM gateway_report_deliveries WHERE message_id = %(id)s", {"id": im.id}
        )
    assert intent == {"destination_id": provider.id}


async def test_completion_advances_fifo_without_later_queued_history(receipt_database: ReceiptDatabase) -> None:
    # Given a claimed first turn and later human inputs already durably queued.
    db = receipt_database
    messages = await seed_shared(db)
    async with transaction(db.first):
        assert [row.id for row in await claim_poll_batch(db.agent.id)] == [messages[0].id]

    # When the first result completes and the next worker claims the queue.
    async with transaction(db.first):
        report = await record_report(db.agent, messages[0].id, "first answer")
    async with transaction(db.second):
        following = await claim_poll_batch(db.agent.id)
        history = await completed_history(following[0])

    # Then only the next input runs with completed prior context, never future inputs.
    assert report is not None
    assert [row.id for row in following] == [messages[1].id]
    assert [(entry.role, entry.content) for entry in history] == [("user", "0"), ("assistant", "first answer")]


async def test_shared_report_cannot_complete_unclaimed_later_turn(receipt_database: ReceiptDatabase) -> None:
    # Given a queue whose head has not yet been claimed.
    db = receipt_database
    messages = await seed_shared(db)
    # When a guest reports a later pending turn.
    async with transaction(db.first):
        report = await record_report(db.agent, messages[1].id, "out of order")
    # Then no later input can bypass the shared claim gate.
    assert report is None
    async with transaction(db.first):
        assert [row.id for row in await claim_poll_batch(db.agent.id)] == [messages[0].id]


async def test_reply_intent_is_not_rederived_and_rejects_forged_destination(receipt_database: ReceiptDatabase) -> None:
    # Given a canonical turn accepted with an independent IM reply intent.
    from dataclasses import replace

    db = receipt_database
    await seed_shared(db)
    async with transaction(db.first):
        await db.first.execute("DELETE FROM gateway_messages WHERE agent_id = %(id)s", {"id": db.agent.id})
        provider = await chat_session_dao.create(
            obj_in={
                "agent_id": db.agent.id,
                "user_id": db.user.id,
                "title": "Slack",
                "source_channel": "slack",
                "external_conv_id": "C-QA",
            }
        )
        prepared = openclaw_routing.PreparedOpenClawMessage(db.agent, "IM input", "primary", None, None, "qa")
        message = await openclaw_routing.enqueue_prepared_openclaw_message(
            prepared,
            sender_user_id=db.user.id,
            conversation_id=str(shared_session_id(db.agent.id)),
            reply_session_id=provider.id,
        )
        assert await gateway_report_delivery_dao.has_outstanding_session(provider.id)
        await claim_poll_batch(db.agent.id)
    # When the turn completes and a relay claims its persisted intent.
    async with transaction(db.first):
        await record_report(db.agent, message.id, "provider answer")
    async with transaction(db.second):
        claims = await gateway_report_delivery_dao.claim_batch(limit=10, lease_seconds=60, now=datetime.now(UTC))
        claim = claims[0]
        authentic = await gateway_report_delivery_dao.lock_claim(claim, now=datetime.now(UTC))
        forged = await gateway_report_delivery_dao.lock_claim(
            replace(claim, destination_id=db.session_id),
            now=datetime.now(UTC),
        )
    # Then completion preserved precisely one destination and leases cannot redirect it.
    assert len(claims) == 1
    assert claim.destination_id == provider.id
    assert authentic is True
    assert forged is False
