from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException
from tests.burst_dispatch_fixtures import Lane, age_pending, claim, lane, linked, report
from tests.channel_submission_fixtures import ChannelDatabase, channel_db, receipt_db
from tests.personal_burst_fixtures import rows, scalar
from tests.test_whatsapp_personal_integration import connected, message_for, target_for

from app.db.session import transaction
from app.records.outbound_plan import OutboundChunk
from app.records.whatsapp_personal import PersonalConnection
from app.services.channels.whatsapp_personal_events import handle_frame
from app.services.channels.whatsapp_personal_intake import accept_message
from app.services.channels.whatsapp_personal_journal import PersonalSendPendingError, authorize_dispatch, enqueue
from app.services.channels.whatsapp_personal_protocol import SendResult

__all__ = ["channel_db", "lane", "linked", "receipt_db"]
QUOTA = 3


async def linked_owner(f: ChannelDatabase) -> PersonalConnection:
    async with transaction(f.db.first):
        owner = await connected(f)
        await f.db.first.execute(
            "UPDATE users SET quota_message_limit=%(limit)s WHERE id=%(id)s", {"limit": QUOTA, "id": f.db.user.id}
        )
    assert isinstance(owner, PersonalConnection)
    return owner


async def exchange(lane: Lane, native: str) -> str:
    """Accept one native self text, then durably send and acknowledge the assistant reply; returns its native ID."""
    f, owner = lane.linked.f, lane.linked.owner
    async with transaction(f.db.first):
        await handle_frame(owner, message_for(owner, native))
    await age_pending(lane, 10)
    turn = (await claim(lane)).message
    assert turn is not None
    assert (turn.status, turn.content, turn.conversation_id) == ("delivered", "self message", str(lane.linked.shared))
    accepted = await rows(
        lane.linked,
        "SELECT batch_id, ordinal, outcome FROM personal_burst_inputs WHERE event_key = %(key)s",
        {"key": f"personal:{owner.auth_epoch}:{native}"},
    )
    assert [(row["batch_id"], row["ordinal"], row["outcome"]) for row in accepted] == [(turn.id, 1, "accepted")]
    answer = f"reply to {native}"
    winner = await report(lane, turn.id, answer)
    assert winner is not None
    assert winner.message.result == answer
    assert await report(lane, turn.id, "duplicate reply") is None
    intents = await rows(
        lane.linked,
        "SELECT d.destination_kind, s.external_conv_id FROM gateway_report_deliveries d "
        "JOIN chat_sessions s ON s.id = d.destination_id WHERE d.message_id = %(id)s",
        {"id": turn.id},
    )
    assert [(row["destination_kind"], row["external_conv_id"]) for row in intents] == [
        ("im", target_for(owner).destination)
    ]
    async with transaction(f.db.first):
        with pytest.raises(PersonalSendPendingError):
            await enqueue(target_for(owner), OutboundChunk(text=answer, key=uuid4()))
    async with transaction(f.db.first):
        command = await authorize_dispatch(owner)
        assert command is not None
    async with transaction(f.db.first):
        ack = SendResult(
            type="send_result",
            id="ack",
            token="test",
            command_id=str(command.command_id),
            message_id=command.message_id,
            outcome="accepted",
        )
        await handle_frame(owner, ack)
    return command.message_id


async def totals(f: ChannelDatabase) -> tuple[object, ...]:
    async with transaction(f.db.second):
        db = f.db.second
        return (
            await db.fetchval("SELECT count(*) FROM channel_event_receipts"),
            await db.fetchval("SELECT quota_messages_used FROM users WHERE id=%(id)s", {"id": f.db.user.id}),
            await db.fetchval("SELECT count(*) FROM gateway_messages"),
            await db.fetchval("SELECT count(*) FROM gateway_report_deliveries"),
            await db.fetchval("SELECT count(*) FROM whatsapp_personal_sends WHERE state='accepted'"),
        )


async def test_first_second_and_third_distinct_texts_are_each_accepted_and_answered(lane: Lane):
    # Given a linked owner whose quota allows exactly three messages.
    f = lane.linked.f
    async with transaction(f.db.first):
        await f.db.first.execute(
            "UPDATE users SET quota_message_limit=%(limit)s WHERE id=%(id)s", {"limit": QUOTA, "id": f.db.user.id}
        )
    # When three distinct native texts each receive an acknowledged assistant reply.
    for native in ("native-first", "native-second", "native-third"):
        await exchange(lane, native)
    # Then each has its own receipt, quota charge, guest turn, reply intent and accepted send.
    assert await totals(f) == (3, 3, 3, 3, 3)


async def test_replay_and_assistant_echo_add_no_charge_after_quota_is_exhausted(lane: Lane):
    # Given three accepted exchanges that consumed the whole quota.
    f, owner = lane.linked.f, lane.linked.owner
    async with transaction(f.db.first):
        await f.db.first.execute(
            "UPDATE users SET quota_message_limit=%(limit)s WHERE id=%(id)s", {"limit": QUOTA, "id": f.db.user.id}
        )
    assistant_ids = [await exchange(lane, native) for native in ("native-first", "native-second", "native-third")]
    # When every native text is replayed and every assistant ID is echoed back.
    async with transaction(f.db.first):
        for native in ("native-first", "native-second", "native-third"):
            await handle_frame(owner, message_for(owner, native))
    for assistant_id in assistant_ids:
        with pytest.raises(HTTPException) as denied:
            async with transaction(f.db.first):
                await handle_frame(owner, message_for(owner, assistant_id))
        assert denied.value.status_code == 403
    # Then nothing further is received, charged or queued, while a genuinely new text is rejected for quota.
    assert await totals(f) == (3, 3, 3, 3, 3)
    async with transaction(f.db.first):
        await handle_frame(owner, message_for(owner, "native-fourth"))
    rejected = await rows(
        lane.linked,
        "SELECT outcome, batch_id, text, chat_message_id, notice_id FROM personal_burst_inputs WHERE event_key = %(key)s",
        {"key": f"personal:{owner.auth_epoch}:native-fourth"},
    )
    assert len(rejected) == 1
    assert (rejected[0]["outcome"], rejected[0]["batch_id"], rejected[0]["text"], rejected[0]["chat_message_id"]) == (
        "quota_rejected",
        None,
        None,
        None,
    )
    assert await scalar(lane.linked, "SELECT count(*) FROM channel_event_receipts") == 4
    assert (
        await scalar(lane.linked, "SELECT quota_messages_used FROM users WHERE id = %(id)s", {"id": f.db.user.id}) == 3
    )
    assert await scalar(lane.linked, "SELECT count(*) FROM chat_messages WHERE role = 'user'") == 3
    assert await scalar(lane.linked, "SELECT count(*) FROM personal_burst_inputs WHERE outcome = 'accepted'") == 3
    notices = await rows(lane.linked, "SELECT * FROM gateway_messages WHERE routing_reason = 'whatsapp_busy'")
    assert len(notices) == 1
    assert (notices[0]["status"], notices[0]["conversation_id"], notices[0]["content"]) == ("completed", None, "")
    assert rejected[0]["notice_id"] == await scalar(
        lane.linked, "SELECT id FROM personal_busy_notices WHERE reason = 'quota'"
    )
    assert await scalar(lane.linked, "SELECT count(*) FROM whatsapp_personal_sends WHERE state = 'accepted'") == 3


@pytest.mark.parametrize("window", ["before_activation", "future"])
async def test_text_outside_the_activation_window_is_not_accepted(channel_db, window):
    # Given a linked owner and a self text stamped outside the accepted window.
    f = channel_db
    owner = await linked_owner(f)
    assert owner.activated_at is not None
    stamp = (
        int(owner.activated_at.timestamp()) - 1
        if window == "before_activation"
        else int(datetime.now(UTC).timestamp()) + 120
    )
    # When it reaches durable intake.
    async with transaction(f.db.first):
        assert not await accept_message(owner, message_for(owner).model_copy(update={"timestamp": stamp}))
    # Then no receipt or charge exists.
    assert await totals(f) == (0, 0, 0, 0, 0)


@pytest.mark.parametrize("authority", ["retired_claim", "auth_epoch"])
async def test_text_from_a_stale_socket_authority_is_denied(channel_db, authority):
    # Given a linked owner and a socket whose claim or auth epoch is no longer current.
    f = channel_db
    owner = await linked_owner(f)
    stale = replace(owner, claim_token=uuid4()) if authority == "retired_claim" else replace(owner, auth_epoch=uuid4())
    # When it submits a fresh self text.
    with pytest.raises(HTTPException) as denied:
        async with transaction(f.db.first):
            await accept_message(stale, message_for(owner))
    # Then the engine denies it without a receipt or charge.
    assert denied.value.status_code == 403
    assert await totals(f) == (0, 0, 0, 0, 0)
