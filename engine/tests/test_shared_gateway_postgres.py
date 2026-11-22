from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import anyio
import pytest

import test_chat_submission_postgres as receipt_fixtures
from app.dao.chat_dao import chat_session_dao
from app.dao.gateway_message_dao import gateway_message_dao
from app.dao.gateway_report_delivery_dao import gateway_report_delivery_dao
from app.db.connection import DbConnection
from app.db.pool import close_pool, init_pool
from app.db.session import transaction
from app.records.gateway_message import GatewayMessageRecord
from app.records.openclaw_dispatch import SharedClaim, SharedDispatchRequest
from app.services import openclaw_routing, openclaw_update_exclusion
from app.services.openclaw_gateway_delivery import record_report
from app.services.openclaw_shared_dispatch import claim_ready_shared
from app.services.openclaw_turns import runtime_session_key
from test_chat_submission_postgres import ReceiptDatabase

receipt_database = receipt_fixtures.receipt_db


@pytest.fixture
async def pooled_database(
    receipt_database: ReceiptDatabase, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AsyncIterator[ReceiptDatabase]:
    """The isolated schema behind the real process pool, with the guest workspace flock kept inside ``tmp_path``."""
    monkeypatch.setattr(
        openclaw_update_exclusion.agent_manager, "_agent_dir", lambda agent_id: tmp_path / str(agent_id)
    )
    await init_pool(conninfo=receipt_database.conninfo, min_size=1, max_size=6)
    try:
        yield receipt_database
    finally:
        await close_pool()


async def seed_human_lane(db: ReceiptDatabase, user_id: UUID | None = None) -> list[GatewayMessageRecord]:
    """A user's (default: the owner's) canonical conversation, one FIFO human lane, holding three pending turns."""
    member = user_id or db.user.id
    async with transaction(db.first):
        session = await chat_session_dao.insert_primary_platform(db.agent.id, member)
        return [
            await gateway_message_dao.create(
                obj_in={
                    "agent_id": db.agent.id,
                    "sender_user_id": member,
                    "conversation_id": str(session.id),
                    "content": str(index),
                    "status": "pending",
                    "created_at": datetime(2026, 10, 20, tzinfo=UTC) + timedelta(seconds=index),
                }
            )
            for index in range(3)
        ]


def lane_of(messages: list[GatewayMessageRecord]) -> UUID:
    return UUID(messages[0].conversation_id or "")


def human_request(messages: list[GatewayMessageRecord], now: datetime | None = None) -> SharedDispatchRequest:
    return SharedDispatchRequest(messages[0].agent_id, now or datetime.now(UTC), lane_of(messages))


async def test_competing_shared_wakes_claim_only_fifo_head(pooled_database: ReceiptDatabase) -> None:
    # Given two workers competing for three shared turns.
    db = pooled_database
    messages = await seed_human_lane(db)
    claimed: list[GatewayMessageRecord] = []

    async def wake() -> None:
        row = await claim_ready_shared(human_request(messages))
        if row:
            claimed.append(row)

    # When wakes run concurrently using independent real connections.
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(wake)
        tasks.start_soon(wake)

    # Then only the earliest turn is executable, even after the normal timeout.
    assert [row.id for row in claimed] == [messages[0].id]
    assert await claim_ready_shared(human_request(messages, datetime.now(UTC) + timedelta(days=1))) is None


async def test_low_level_shared_claim_takes_the_lane_lock_and_only_the_fifo_head(
    receipt_database: ReceiptDatabase,
) -> None:
    # Given two independent connections racing the DAO claim itself.
    db = receipt_database
    messages = await seed_human_lane(db)
    claimed: list[GatewayMessageRecord] = []

    async def claim(connection: DbConnection) -> None:
        async with transaction(connection):
            lane = lane_of(messages)
            assert await gateway_message_dao.lock_human_lane(db.agent.id, lane)
            row = await gateway_message_dao.claim_head(
                SharedClaim(
                    messages[0].id,
                    db.agent.id,
                    str(lane),
                    messages[0].content,
                    None,
                    None,
                    None,
                    datetime.now(UTC),
                )
            )
            if row:
                claimed.append(row)

    # When both claim at once.
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(claim, db.first)
        tasks.start_soon(claim, db.second)

    # Then the serialized lane yields exactly one, the oldest.
    assert [row.id for row in claimed] == [messages[0].id]


async def test_reply_intent_waits_for_completion_and_equal_human_inputs_remain_distinct(
    receipt_database: ReceiptDatabase,
) -> None:
    # Given a canonical transcript and an independent provider session.
    db = receipt_database
    messages = await seed_human_lane(db)
    prepared = openclaw_routing.PreparedOpenClawMessage(db.agent, "same", "primary", None, "qa")
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
            prepared, sender_user_id=db.user.id, conversation_id=str(lane_of(messages))
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


async def test_completion_advances_fifo_with_stable_runtime_session(pooled_database: ReceiptDatabase) -> None:
    # Given a claimed first turn and later human inputs already durably queued.
    db = pooled_database
    messages = await seed_human_lane(db)
    first = await claim_ready_shared(human_request(messages))
    assert first is not None
    assert first.id == messages[0].id

    # When the first result completes and the next worker claims the queue.
    async with transaction(db.first):
        report = await record_report(db.agent, messages[0].id, "first answer")
    following = await claim_ready_shared(human_request(messages))
    assert following is not None
    # Then only the next input runs, in the same native gateway session.
    assert report is not None
    assert following.id == messages[1].id
    assert runtime_session_key(first) == runtime_session_key(following)


async def test_shared_report_cannot_complete_unclaimed_later_turn(pooled_database: ReceiptDatabase) -> None:
    # Given a queue whose head has not yet been claimed.
    db = pooled_database
    messages = await seed_human_lane(db)
    # When a guest reports a later pending turn.
    async with transaction(db.first):
        report = await record_report(db.agent, messages[1].id, "out of order")
    # Then no later input can bypass the shared claim gate.
    assert report is None
    first = await claim_ready_shared(human_request(messages))
    assert first is not None
    assert first.id == messages[0].id


async def test_reply_intent_is_not_rederived_and_rejects_forged_destination(pooled_database: ReceiptDatabase) -> None:
    # Given a canonical turn accepted with an independent IM reply intent.
    from dataclasses import replace

    db = pooled_database
    messages = await seed_human_lane(db)
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
        prepared = openclaw_routing.PreparedOpenClawMessage(db.agent, "IM input", "primary", None, "qa")
        message = await openclaw_routing.enqueue_prepared_openclaw_message(
            prepared,
            sender_user_id=db.user.id,
            conversation_id=str(lane_of(messages)),
            reply_session_id=provider.id,
        )
        assert await gateway_report_delivery_dao.has_outstanding_session(provider.id)
    claimed = await claim_ready_shared(human_request(messages))
    assert claimed is not None
    assert claimed.id == message.id
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


@pytest.mark.parametrize("sender", ["other_user", "no_user"])
async def test_canonical_enqueue_refuses_any_sender_but_the_owner(
    receipt_database: ReceiptDatabase, sender: str
) -> None:
    # Given the owner's canonical lane and another member of the agent.
    db = receipt_database
    messages = await seed_human_lane(db)
    other = uuid4()
    async with transaction(db.first):
        await db.first.execute("INSERT INTO users (id, display_name) VALUES (%(id)s, 'Other')", {"id": other})
    prepared = openclaw_routing.PreparedOpenClawMessage(db.agent, "not yours", "primary", None, "qa")
    # When a turn from anyone but the owner is queued into that lane.
    with pytest.raises(ValueError, match="owner"):
        async with transaction(db.first):
            _ = await openclaw_routing.enqueue_prepared_openclaw_message(
                prepared,
                sender_user_id=other if sender == "other_user" else None,
                conversation_id=str(lane_of(messages)),
            )
    # Then nothing is queued, so the owner filter on every delivery stays the sender filter.
    async with transaction(db.second):
        count = await db.second.fetchval(
            "SELECT count(*) FROM gateway_messages WHERE agent_id = %(id)s", {"id": db.agent.id}
        )
    assert count == len(messages)
