from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.api import background_tasks
from channel_shared_postgres_fixtures import canonical_sessions, live_channel, post_slack, schema_db

__all__ = ["live_channel", "schema_db"]


async def test_signed_direct_humans_get_their_own_canonical_transcripts_with_distinct_reply_intents(live_channel):
    # Given two linked humans who are not the agent creator.
    live = live_channel
    # When each submits a signed direct Slack event through the HTTP router.
    responses = [
        await post_slack(live, text="first", channel="D0"),
        await post_slack(live, sender="U1", text="second", channel="D1"),
    ]
    await background_tasks.drain_background_tasks()
    # Then each human owns one private canonical conversation, and the agent creator owns none.
    assert [response.status_code for response in responses] == [200, 200]
    canonical = await canonical_sessions(live)
    first, second = live.users[0], live.users[1]
    assert set(canonical) == {first, second}
    assert canonical[first] != canonical[second]
    # And each persisted message sits in its sender's own conversation, with its reply intent aimed at the IM session.
    messages = await live.db.fetchall("SELECT user_id, conversation_id, content FROM chat_messages ORDER BY created_at")
    assert [(row["user_id"], row["conversation_id"], row["content"]) for row in messages] == [
        (first, canonical[first], "first"),
        (second, canonical[second], "second"),
    ]
    deliveries = await live.db.fetchall(
        "SELECT g.conversation_id, s.source_channel, s.external_conv_id "
        "FROM gateway_messages g JOIN gateway_report_deliveries d ON d.message_id = g.id "
        "JOIN chat_sessions s ON s.id = d.destination_id ORDER BY g.created_at"
    )
    assert [(row["conversation_id"], row["source_channel"], row["external_conv_id"]) for row in deliveries] == [
        (canonical[first], "slack", "slack_D0"),
        (canonical[second], "slack", "slack_D1"),
    ]
    # And each realtime event reaches only its owner's sockets.
    assert live.published.await_count == 2
    assert {(call.args[1], call.args[3]) for call in live.published.await_args_list} == {
        (canonical[first], str(first)),
        (canonical[second], str(second)),
    }


async def test_signed_group_message_stays_in_its_group_session(live_channel):
    # Given a linked human writing in a group channel.
    live = live_channel
    # When the signed group event is accepted.
    response = await post_slack(live, text="group hello", channel="C0")
    await background_tasks.drain_background_tasks()
    # Then the message lives in the group's own session, no canonical conversation is opened, and nothing is pushed.
    assert response.status_code == 200
    messages = await live.db.fetchall("SELECT conversation_id, content FROM chat_messages")
    assert [row["content"] for row in messages] == ["group hello"]
    group = await live.db.fetchone(
        "SELECT is_group, source_channel FROM chat_sessions WHERE id = %(id)s", {"id": messages[0]["conversation_id"]}
    )
    assert group == {"is_group": True, "source_channel": "slack"}
    assert await canonical_sessions(live) == {}
    live.published.assert_not_awaited()


@pytest.mark.parametrize(
    ("revocation", "expected_status"),
    [("identity", 401), ("membership", 401), ("password", 403), ("tenant", 403), ("access", 403), ("unbound", 200)],
)
async def test_direct_http_checks_fresh_authority_after_an_accepted_turn(live_channel, revocation, expected_status):
    # Given an already accepted human whose authority is then revoked in PostgreSQL.
    live = live_channel
    assert (await post_slack(live)).status_code == 200
    match revocation:
        case "identity":
            await live.db.execute(
                "UPDATE identities SET is_active = false WHERE id = %(id)s", {"id": live.identities[0]}
            )
        case "membership":
            await live.db.execute("UPDATE users SET is_active = false WHERE id = %(id)s", {"id": live.users[0]})
        case "password":
            await live.db.execute(
                "UPDATE identities SET must_change_password = true WHERE id = %(id)s", {"id": live.identities[0]}
            )
        case "tenant":
            await live.db.execute(
                "UPDATE tenants SET is_active = false WHERE id = %(id)s", {"id": live.agent.tenant_id}
            )
        case "access":
            await live.db.execute("UPDATE agents SET access_mode = 'private' WHERE id = %(id)s", {"id": live.agent.id})
        case "unbound":
            await live.db.execute("UPDATE org_members SET user_id = NULL WHERE user_id = %(id)s", {"id": live.users[0]})
        case _:
            raise AssertionError(revocation)
    # When a second signed direct message arrives on the same provider conversation.
    response = await post_slack(live, text="must-not-enter-context")
    # Then no additional user message, inbox message or delivery intent is accepted.
    assert response.status_code == expected_status
    counts = await live.db.fetchall(
        "SELECT count(*) AS count FROM chat_messages UNION ALL "
        "SELECT count(*) FROM gateway_messages UNION ALL SELECT count(*) FROM gateway_report_deliveries"
    )
    assert counts == [{"count": 1}] * 3


async def test_signed_unbound_direct_sender_is_acknowledged_without_canonical_access(live_channel, monkeypatch):
    # Given an unbound human and a history boundary that must not be reached.
    from app.dao.chat_dao import chat_message_dao

    live = live_channel
    history = AsyncMock(side_effect=AssertionError("Unbound sender reached conversation history"))
    monkeypatch.setattr(chat_message_dao, "list_recent", history)
    # When the provider delivers a validly signed direct message.
    response = await post_slack(live, sender="U_UNBOUND")
    await background_tasks.drain_background_tasks()
    # Then receipt is acknowledged without accepting or exposing any conversation state.
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    history.assert_not_awaited()
    counts = await live.db.fetchall(
        "SELECT count(*) AS count FROM chat_sessions UNION ALL SELECT count(*) FROM chat_messages UNION ALL "
        "SELECT count(*) FROM gateway_messages UNION ALL SELECT count(*) FROM gateway_report_deliveries"
    )
    assert counts == [{"count": 0}] * 4
    live.published.assert_not_awaited()


async def test_invalid_provider_signature_never_creates_transcript(live_channel):
    # Given a configured real provider boundary.
    live = live_channel
    # When an invalid signature is submitted.
    response = await post_slack(live, valid_signature=False)
    # Then authentication rejects before a transcript or inbox exists.
    assert response.status_code == 401
    assert await live.db.fetchall("SELECT id FROM chat_sessions") == []
    assert await live.db.fetchall("SELECT id FROM gateway_messages") == []


async def test_reply_intent_failure_rolls_back_user_and_inbox(live_channel, monkeypatch):
    # Given a provider turn whose durable reply intent cannot be written.
    from app.dao.gateway_report_delivery_dao import gateway_report_delivery_dao

    live = live_channel
    monkeypatch.setattr(
        gateway_report_delivery_dao, "create_intent", AsyncMock(side_effect=RuntimeError("intent failed"))
    )
    # When the signed HTTP turn reaches the transactional enqueue boundary.
    response = await post_slack(live)
    await background_tasks.drain_background_tasks()
    # Then neither half of the turn nor a realtime acceptance event survives.
    assert response.status_code == 500
    counts = await live.db.fetchall(
        "SELECT count(*) AS count FROM chat_messages UNION ALL "
        "SELECT count(*) FROM gateway_messages UNION ALL SELECT count(*) FROM gateway_report_deliveries"
    )
    assert counts == [{"count": 0}] * 3
    live.published.assert_not_awaited()
