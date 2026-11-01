from dataclasses import replace

import pytest
from tests.personal_burst_fixtures import (
    LIMITS,
    LinkedBurst,
    channel_db,
    linked,
    receipt_db,
    rows,
    scalar,
    submit,
    use_limits,
)

from app.db.session import transaction
from app.services.channels.personal_burst_outcome import PersonalRejected, PersonalReplayed

__all__ = ["channel_db", "linked", "receipt_db"]

MARKER = "rejected-body-marker"
NOTICES = "SELECT * FROM gateway_messages WHERE routing_reason = 'whatsapp_busy'"


async def quota_used(linked: LinkedBurst) -> object:
    return await scalar(linked, "SELECT quota_messages_used FROM users WHERE id = %(id)s", {"id": linked.f.db.user.id})


async def test_capacity_rejection_is_durable_uncharged_and_covered_by_one_static_notice(
    linked: LinkedBurst, monkeypatch
):
    # Given a lane that already holds its one allowed waiting original.
    use_limits(monkeypatch, replace(LIMITS, pending_count=1))
    _ = await submit(linked, "n1", "first")
    batch_before = await rows(linked, "SELECT * FROM personal_burst_batches")

    # When a second original arrives.
    result = await submit(linked, "n2", MARKER)

    # Then it is a typed handled-not-accepted outcome with a freshly created notice.
    notice = (await rows(linked, NOTICES))[0]
    assert isinstance(result.personal, PersonalRejected)
    assert (result.personal.outcome, result.personal.notice_created) == ("capacity_rejected", True)
    # And the static source is a completed machine row with no conversation, sender or content.
    assert (notice["status"], notice["conversation_id"], notice["sender_user_id"], notice["sender_agent_id"]) == (
        "completed",
        None,
        None,
        None,
    )
    assert notice["content"] == ""
    result_text = notice["result"]
    assert isinstance(result_text, str)
    assert result_text.strip()
    # And it has one pinned IM intent for the reply session and cooldown metadata in the same commit.
    intent = await rows(
        linked, "SELECT * FROM gateway_report_deliveries WHERE message_id = %(id)s", {"id": notice["id"]}
    )
    reply = await scalar(linked, "SELECT id FROM chat_sessions WHERE source_channel = 'whatsapp'")
    assert [(i["destination_kind"], i["destination_id"]) for i in intent] == [("im", reply)]
    assert await scalar(linked, "SELECT reason FROM personal_busy_notices") == "capacity"
    # And nothing was charged, stored as transcript, retained as a body, or changed in the open batch.
    assert await quota_used(linked) == 1
    assert await scalar(linked, "SELECT count(*) FROM chat_messages WHERE role = 'user'") == 1
    assert await rows(linked, "SELECT * FROM personal_burst_batches") == batch_before
    outcome = (await rows(linked, "SELECT * FROM personal_burst_inputs WHERE outcome <> 'accepted'"))[0]
    assert (outcome["batch_id"], outcome["text"], outcome["chat_message_id"]) == (None, None, None)
    assert outcome["notice_id"] == await scalar(linked, "SELECT id FROM personal_busy_notices")
    assert await scalar(linked, "SELECT count(*) FROM channel_event_receipts") == 2
    for found in (
        "SELECT count(*) FROM chat_messages WHERE position(%(marker)s in content) > 0",
        "SELECT count(*) FROM chat_sessions WHERE position(%(marker)s in title) > 0",
        "SELECT count(*) FROM gateway_messages WHERE position(%(marker)s in content || coalesce(result, '')) > 0",
        "SELECT count(*) FROM personal_burst_inputs WHERE position(%(marker)s in coalesce(text, '')) > 0",
    ):
        assert await scalar(linked, found, {"marker": MARKER}) == 0


async def test_byte_backlog_rejects_with_capacity(linked: LinkedBurst, monkeypatch):
    # Given a raw byte backlog of 30 with one 20-byte original waiting.
    use_limits(monkeypatch, replace(LIMITS, batch_bytes=30, pending_bytes=30))
    _ = await submit(linked, "n1", "a" * 20)

    # When another 20-byte original (within the body limit) would exceed the backlog.
    result = await submit(linked, "n2", "b" * 20)

    # Then it is rejected for capacity.
    assert isinstance(result.personal, PersonalRejected)
    assert result.personal.outcome == "capacity_rejected"


@pytest.mark.parametrize(
    ("text", "admitted"),
    [("a" * 10, True), ("a" * 11, False), ("é" * 5, True), ("é" * 6, False)],
)
async def test_batch_body_limit_is_exact_utf8_bytes_and_never_truncates(
    linked: LinkedBurst, monkeypatch, text: str, admitted: bool
):
    # Given a ten-byte batch body limit.
    use_limits(monkeypatch, replace(LIMITS, batch_bytes=10, pending_bytes=100))

    # When one original of that many UTF-8 bytes (or one more) arrives.
    result = await submit(linked, "n1", text)

    # Then it is admitted whole at the limit and explicitly rejected above it.
    if admitted:
        assert await scalar(linked, "SELECT text FROM personal_burst_inputs") == text
    else:
        assert isinstance(result.personal, PersonalRejected)
        assert result.personal.outcome == "oversize_rejected"
        assert await scalar(linked, "SELECT count(*) FROM personal_burst_batches") == 0
        assert await scalar(linked, "SELECT reason FROM personal_busy_notices") == "oversize"


async def test_quota_exhaustion_is_a_durable_rejection_not_a_raised_error(linked: LinkedBurst):
    # Given a sender with exactly one message of quota.
    async with transaction(linked.f.db.first):
        await linked.f.db.first.execute("UPDATE users SET quota_message_limit = 1")
    _ = await submit(linked, "n1", "first")

    # When the next original arrives.
    result = await submit(linked, "n2", MARKER)

    # Then it is rejected for quota with no extra charge, transcript, member or batch change.
    assert isinstance(result.personal, PersonalRejected)
    assert result.personal.outcome == "quota_rejected"
    assert await quota_used(linked) == 1
    assert await scalar(linked, "SELECT count(*) FROM chat_messages WHERE role = 'user'") == 1
    assert await scalar(linked, "SELECT count(*) FROM personal_burst_inputs WHERE outcome = 'accepted'") == 1
    assert await scalar(linked, "SELECT reason FROM personal_busy_notices") == "quota"


async def test_capacity_is_judged_before_quota(linked: LinkedBurst, monkeypatch):
    # Given a full lane and a sender who has also exhausted quota.
    use_limits(monkeypatch, replace(LIMITS, pending_count=1))
    async with transaction(linked.f.db.first):
        await linked.f.db.first.execute("UPDATE users SET quota_message_limit = 1")
    _ = await submit(linked, "n1", "first")

    # When another original arrives.
    result = await submit(linked, "n2", "second")

    # Then capacity (not quota) is the recorded reason.
    assert isinstance(result.personal, PersonalRejected)
    assert result.personal.outcome == "capacity_rejected"


async def test_second_rejection_inside_the_cooldown_creates_no_extra_notice(linked: LinkedBurst, monkeypatch):
    # Given one rejection that already created a notice.
    use_limits(monkeypatch, replace(LIMITS, pending_count=1))
    _ = await submit(linked, "n1", "first")
    first = await submit(linked, "n2", "second")
    assert isinstance(first.personal, PersonalRejected)

    # When another original is rejected within the cooldown.
    second = await submit(linked, "n3", "third")

    # Then it links the covering notice and creates no extra gateway row or intent.
    assert isinstance(second.personal, PersonalRejected)
    assert (second.personal.notice_created, second.personal.notice_id) == (False, first.personal.notice_id)
    assert len(await rows(linked, NOTICES)) == 1
    assert await scalar(linked, "SELECT count(*) FROM gateway_report_deliveries") == 2
    assert await scalar(linked, "SELECT count(*) FROM personal_burst_inputs WHERE notice_id IS NOT NULL") == 2


async def test_rejection_after_the_cooldown_creates_a_new_notice(linked: LinkedBurst, monkeypatch):
    # Given a notice older than the cooldown.
    use_limits(monkeypatch, replace(LIMITS, pending_count=1))
    _ = await submit(linked, "n1", "first")
    _ = await submit(linked, "n2", "second")
    async with transaction(linked.f.db.first):
        await linked.f.db.first.execute(
            "UPDATE personal_busy_notices SET created_at = created_at - make_interval(secs => %(s)s)",
            {"s": LIMITS.notice_cooldown_seconds + 1},
        )

    # When another original is rejected.
    result = await submit(linked, "n3", "third")

    # Then a second notice is created.
    assert isinstance(result.personal, PersonalRejected)
    assert result.personal.notice_created
    assert len(await rows(linked, NOTICES)) == 2


async def test_replayed_rejection_stays_rejected_even_after_capacity_returns(linked: LinkedBurst, monkeypatch):
    # Given a rejected native event.
    use_limits(monkeypatch, replace(LIMITS, pending_count=1))
    _ = await submit(linked, "n1", "first")
    _ = await submit(linked, "n2", "second")
    snapshot = (
        await rows(linked, "SELECT * FROM personal_burst_batches"),
        await rows(linked, "SELECT * FROM personal_busy_notices"),
        await quota_used(linked),
    )

    # When capacity is plentiful and the same event is replayed.
    use_limits(monkeypatch, LIMITS)
    replay = await submit(linked, "n2", "second")

    # Then it is reported as the stored rejection and admits, charges and notifies nothing.
    assert replay.replayed
    assert replay.personal == PersonalReplayed(stored="capacity_rejected")
    assert snapshot == (
        await rows(linked, "SELECT * FROM personal_burst_batches"),
        await rows(linked, "SELECT * FROM personal_busy_notices"),
        await quota_used(linked),
    )
    assert await scalar(linked, "SELECT count(*) FROM personal_burst_inputs WHERE outcome = 'accepted'") == 1
