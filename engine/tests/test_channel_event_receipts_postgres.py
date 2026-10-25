from dataclasses import replace
from unittest.mock import AsyncMock

import anyio
import pytest
from fastapi import HTTPException
from tests.channel_submission_fixtures import ChannelDatabase, channel_db, receipt_db

__all__ = ["channel_db", "receipt_db"]

from app.dao.channel_event_receipt_dao import channel_event_receipt_dao
from app.db.connection import DbConnection
from app.db.session import get_connection, transaction
from app.services.channel_user_service import ChannelUserResolutionError
from app.services.channels import submissions
from app.services.channels.submissions import AcceptedChannelEvent, ChannelEventConflictError, accept_channel_event
from app.services.quota_guard import AgentExpired, QuotaExceeded
from app.services.shared_chat import shared_session_id


async def counts(fixture: ChannelDatabase):
    async with transaction(fixture.db.first):
        result = await fixture.db.first.fetchone(
            "SELECT (SELECT count(*) FROM channel_event_receipts) AS receipts, "
            "(SELECT count(*) FROM chat_messages) AS messages, "
            "(SELECT count(*) FROM gateway_messages) AS enqueued, "
            "(SELECT count(*) FROM gateway_report_deliveries) AS intents, "
            "(SELECT quota_messages_used FROM users WHERE id = %(sender)s) AS charged, "
            "(SELECT quota_messages_used FROM users WHERE id = %(creator)s) AS creator_charged",
            {"sender": fixture.sender_id, "creator": fixture.db.user.id},
        )
        assert result is not None
        return result


async def test_native_replay_serializes_before_session_locks(channel_db, monkeypatch):
    # Given two real connections racing the same native reservation.
    fixture = channel_db
    reserved, attempted, release = anyio.Event(), anyio.Event(), anyio.Event()
    original = channel_event_receipt_dao.reserve
    accepted: list[AcceptedChannelEvent] = []

    async def gated_reserve(receipt):
        if get_connection() is fixture.db.second:
            attempted.set()
        result = await original(receipt)
        if get_connection() is fixture.db.first:
            reserved.set()
            await release.wait()
        return result

    async def accept(connection: DbConnection):
        async with transaction(connection):
            result = await accept_channel_event(fixture.event)
        accepted.append(result)

    monkeypatch.setattr(channel_event_receipt_dao, "reserve", gated_reserve)
    # When the second connection demonstrably blocks on the receipt primary key.
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(accept, fixture.db.first)
            await reserved.wait()
            tasks.start_soon(accept, fixture.db.second)
            await attempted.wait()
            while True:
                row = await fixture.db.first.fetchone(
                    "SELECT pg_blocking_pids(%(pid)s) AS blockers", {"pid": fixture.db.second.raw.info.backend_pid}
                )
                if row and row["blockers"]:
                    break
                await anyio.lowlevel.checkpoint()
            release.set()
    # Then one accepted turn owns every durable side effect and the actual sender's charge.
    assert await counts(fixture) == {
        "receipts": 1,
        "messages": 1,
        "enqueued": 1,
        "intents": 1,
        "charged": 1,
        "creator_charged": 0,
    }
    assert sorted(result.replayed for result in accepted) == [False, True]


async def test_enqueue_failure_rolls_back_receipt_quota_transcript_sessions_and_intent(channel_db, monkeypatch):
    # Given a failure after the real enqueue and reply-intent writes.
    fixture = channel_db
    enqueue = submissions.enqueue_prepared_openclaw_message

    async def fail_after_enqueue(*args, **kwargs):
        await enqueue(*args, **kwargs)
        raise RuntimeError("injected acceptance failure")

    monkeypatch.setattr(submissions, "enqueue_prepared_openclaw_message", fail_after_enqueue)
    # When the owning transaction fails.
    with pytest.raises(RuntimeError, match="injected acceptance failure"):
        async with transaction(fixture.db.first):
            await accept_channel_event(fixture.event)
    # Then nothing was accepted, including the newly created provider/shared sessions.
    assert await counts(fixture) == {
        "receipts": 0,
        "messages": 0,
        "enqueued": 0,
        "intents": 0,
        "charged": 0,
        "creator_charged": 0,
    }
    async with transaction(fixture.db.first):
        assert await fixture.db.first.fetchval("SELECT count(*) FROM chat_sessions") == 1


@pytest.mark.parametrize(("group", "owner"), [(False, "sender"), (True, "creator"), (True, "sender")])
async def test_distinct_native_ids_preserve_transcript_and_reply_routing(channel_db, group, owner):
    # Given identical text with two distinct native IDs.
    fixture = channel_db
    event = replace(fixture.event, is_group=group, group_session_owner=owner)
    # When both events are accepted.
    for key in ("native-1", "native-2"):
        async with transaction(fixture.db.first):
            await accept_channel_event(replace(event, event_key=key))
    # Then both enqueue, with the provider reply session distinct from the shared private transcript.
    assert await counts(fixture) == {
        "receipts": 2,
        "messages": 2,
        "enqueued": 2,
        "intents": 2,
        "charged": 2,
        "creator_charged": 0,
    }
    async with transaction(fixture.db.first):
        rows = await fixture.db.first.fetchall(
            "SELECT g.sender_user_id, g.conversation_id, s.id, s.is_group, s.user_id, s.last_message_at "
            "FROM gateway_messages g JOIN gateway_report_deliveries d ON d.message_id = g.id "
            "JOIN chat_sessions s ON s.id = d.destination_id"
        )
        for row in rows:
            assert row["sender_user_id"] == fixture.sender_id
            assert row["is_group"] is group
            assert row["user_id"] == (fixture.db.user.id if group and owner == "creator" else fixture.sender_id)
            assert row["conversation_id"] == str(row["id"] if group else shared_session_id(event.agent_id))


async def test_replay_survives_completion_cleanup_quota_exhaustion_and_expiry(channel_db, monkeypatch):
    # Given an accepted turn whose transcript, gateway row, and sessions were subsequently removed.
    fixture = channel_db
    async with transaction(fixture.db.first):
        await accept_channel_event(fixture.event)
        await fixture.db.first.execute("DELETE FROM gateway_report_deliveries")
        await fixture.db.first.execute("DELETE FROM gateway_messages")
        await fixture.db.first.execute("DELETE FROM chat_messages")
        await fixture.db.first.execute("DELETE FROM chat_sessions")
        await fixture.db.first.execute("UPDATE users SET quota_message_limit = 1")
        await fixture.db.first.execute("UPDATE agents SET is_expired = TRUE, expires_at = now() - interval '1 day'")
    prepare = AsyncMock(side_effect=AssertionError("replay must not select a model"))
    monkeypatch.setattr(submissions, "prepare_openclaw_message", prepare)
    # When the native event is redelivered after expiry and quota exhaustion.
    async with transaction(fixture.db.second):
        result = await accept_channel_event(fixture.event, content="changed enrichment", saved_content="new local path")
    # Then only the retained receipt is consulted, and no state is recreated or charged.
    assert result.replayed
    assert await counts(fixture) == {
        "receipts": 1,
        "messages": 0,
        "enqueued": 0,
        "intents": 0,
        "charged": 1,
        "creator_charged": 0,
    }
    prepare.assert_not_awaited()


@pytest.mark.parametrize("field", ["sender_id", "conversation_id", "text", "kind", "media_ids", "is_group"])
async def test_native_identity_reuse_with_changed_payload_conflicts(channel_db, field):
    # Given an accepted native identity.
    fixture = channel_db
    async with transaction(fixture.db.first):
        await accept_channel_event(fixture.event)
    changed = {
        "sender_id": "15550001",
        "conversation_id": "changed",
        "text": "changed",
        "kind": "image",
        "media_ids": ("native-media",),
        "is_group": True,
    }
    if field == "sender_id":
        async with transaction(fixture.db.first):
            await fixture.db.first.execute("UPDATE org_members SET external_id = 'other-sender'")
        changed[field] = "other-sender"
    # When the same receipt key carries a different authenticated native payload.
    with pytest.raises(ChannelEventConflictError):
        async with transaction(fixture.db.second):
            await accept_channel_event(replace(fixture.event, **{field: changed[field]}))
    # Then the original accepted turn remains unchanged.
    assert (await counts(fixture))["charged"] == 1


@pytest.mark.parametrize("revoked", ["channel", "user", "provider", "member", "tenant", "permission"])
async def test_replay_rechecks_revoked_authorization(channel_db, monkeypatch, revoked):
    # Given an accepted receipt and an independently revoked authorization source.
    fixture = channel_db
    statements = {
        "channel": "UPDATE channel_configs SET is_configured = FALSE",
        "user": "UPDATE users SET is_active = FALSE",
        "provider": "UPDATE identity_providers SET is_active = FALSE",
        "member": "UPDATE org_members SET status = 'inactive'",
        "tenant": "UPDATE tenants SET is_active = FALSE",
        "permission": "UPDATE agents SET access_mode = 'private'",
    }
    async with transaction(fixture.db.first):
        await accept_channel_event(fixture.event)
        await fixture.db.first.execute(statements[revoked])
    lookup = AsyncMock(side_effect=AssertionError("revoked replay must not read a receipt"))
    monkeypatch.setattr(channel_event_receipt_dao, "reserve", lookup)
    # When the provider redelivers the formerly accepted event.
    with pytest.raises((HTTPException, ChannelUserResolutionError)):
        async with transaction(fixture.db.second):
            await accept_channel_event(fixture.event)
    # Then authorization rejected it before durable replay.
    lookup.assert_not_awaited()


@pytest.mark.parametrize("policy", ["expiry", "quota", "admin"])
async def test_new_acceptance_obeys_expiry_quota_and_admin_exemption(channel_db, policy):
    # Given a new event subject to a current turn policy.
    fixture = channel_db
    async with transaction(fixture.db.first):
        if policy == "expiry":
            await fixture.db.first.execute("UPDATE agents SET is_expired = TRUE")
        else:
            await fixture.db.first.execute("UPDATE users SET quota_message_limit = 0")
        if policy == "admin":
            await fixture.db.first.execute(
                "UPDATE users SET role = 'org_admin' WHERE id = %(id)s", {"id": fixture.sender_id}
            )
    # When a new native event is submitted.
    if policy == "admin":
        async with transaction(fixture.db.second):
            await accept_channel_event(fixture.event)
    else:
        with pytest.raises(AgentExpired if policy == "expiry" else QuotaExceeded):
            async with transaction(fixture.db.second):
                await accept_channel_event(fixture.event)
    # Then admins are uncharged, while rejected new turns have no durable effects.
    state = await counts(fixture)
    assert state["charged"] == 0
    assert state["receipts"] == (1 if policy == "admin" else 0)
