from dataclasses import replace
from uuid import uuid4

import anyio
import pytest
from tests.channel_submission_fixtures import channel_db, receipt_db

__all__ = ["channel_db", "receipt_db"]
from tests.test_channel_event_receipts_postgres import counts

from app.dao.channel_event_receipt_dao import channel_event_receipt_dao
from app.db.session import get_connection, transaction
from app.records.channel_event_receipt import ChannelEventReceiptRecord
from app.services.channels import submissions
from app.services.channels.submissions import ChannelEventConflictError, accept_channel_event


async def test_distinct_native_events_serialize_first_conversation_creation(channel_db, monkeypatch):
    # Given separate native IDs racing to create the same provider/shared sessions.
    fixture = channel_db
    locked, attempted, release = anyio.Event(), anyio.Event(), anyio.Event()
    original = channel_event_receipt_dao.lock_conversation

    async def gated_lock(receipt, conversation_id):
        if get_connection() is fixture.db.second:
            attempted.set()
        await original(receipt, conversation_id)
        if get_connection() is fixture.db.first:
            locked.set()
            await release.wait()

    async def accept(connection, key):
        async with transaction(connection):
            await accept_channel_event(replace(fixture.event, event_key=key))

    monkeypatch.setattr(channel_event_receipt_dao, "lock_conversation", gated_lock)
    # When both reservations succeed but conversation creation is serialized.
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(accept, fixture.db.first, "first")
            await locked.wait()
            tasks.start_soon(accept, fixture.db.second, "second")
            await attempted.wait()
            while True:
                row = await fixture.db.first.fetchone(
                    "SELECT pg_blocking_pids(%(pid)s) AS blockers", {"pid": fixture.db.second.raw.info.backend_pid}
                )
                if row and row["blockers"]:
                    break
                await anyio.lowlevel.checkpoint()
            release.set()
    # Then both distinct events are accepted without losing either to a session-creation race.
    assert await counts(fixture) == {
        "receipts": 2,
        "messages": 2,
        "enqueued": 2,
        "intents": 2,
        "charged": 2,
        "creator_charged": 0,
    }
    async with transaction(fixture.db.first):
        assert await fixture.db.first.fetchval("SELECT count(*) FROM chat_sessions") == 3


async def test_redelivery_can_accept_after_a_rolled_back_enqueue(channel_db, monkeypatch):
    # Given an earlier attempt that failed after writing the real gateway row and intent.
    fixture = channel_db
    enqueue = submissions.enqueue_prepared_openclaw_message

    async def fail(*args, **kwargs):
        await enqueue(*args, **kwargs)
        raise RuntimeError("acceptance interrupted")

    with monkeypatch.context() as failed_attempt:
        failed_attempt.setattr(submissions, "enqueue_prepared_openclaw_message", fail)
        with pytest.raises(RuntimeError, match="acceptance interrupted"):
            async with transaction(fixture.db.first):
                await accept_channel_event(fixture.event)
    # When the provider redelivers after the transient failure clears.
    async with transaction(fixture.db.second):
        result = await accept_channel_event(fixture.event)
    # Then this is a new acceptance, with exactly one committed charge and turn.
    assert not result.replayed
    assert await counts(fixture) == {
        "receipts": 1,
        "messages": 1,
        "enqueued": 1,
        "intents": 1,
        "charged": 1,
        "creator_charged": 0,
    }


async def test_receipts_end_with_connector_lifetime_not_turn_lifetime(channel_db):
    # Given an accepted turn attached to a connector configuration.
    fixture = channel_db
    async with transaction(fixture.db.first):
        await accept_channel_event(fixture.event)
    # When that connector is deleted.
    async with transaction(fixture.db.second):
        await fixture.db.second.execute("DELETE FROM channel_configs WHERE id = %(id)s", {"id": fixture.config.id})
    # Then only the receipt lifetime ends; the independent transcript and gateway turn survive.
    state = await counts(fixture)
    assert state["receipts"] == 0
    assert (state["messages"], state["enqueued"], state["charged"]) == (1, 1, 1)


@pytest.mark.parametrize("field", ["agent_id", "tenant_id"])
async def test_receipt_identity_mismatch_is_a_typed_conflict(channel_db, field):
    # Given a retained receipt belonging to a different agent or tenant identity.
    fixture = channel_db
    async with transaction(fixture.db.first):
        await accept_channel_event(fixture.event)
        row = await fixture.db.first.fetchone("SELECT * FROM channel_event_receipts")
        assert row is not None
        accepted = ChannelEventReceiptRecord.from_row(row)
        await fixture.db.first.execute("DELETE FROM channel_event_receipts")
        assert await channel_event_receipt_dao.reserve(replace(accepted, **{field: uuid4()}))
    # When the authenticated native identity is redelivered.
    with pytest.raises(ChannelEventConflictError):
        async with transaction(fixture.db.second):
            await accept_channel_event(fixture.event)
    # Then mismatched ownership never becomes a replay or a second charge.
    assert (await counts(fixture))["charged"] == 1
