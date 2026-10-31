from dataclasses import replace
from uuid import uuid4

import anyio
import pytest
from fastapi import HTTPException
from tests.personal_burst_fixtures import (
    LIMITS,
    LinkedBurst,
    age_batch,
    channel_db,
    event_for,
    linked,
    only_batch,
    receipt_db,
    rows,
    scalar,
    submit,
    use_limits,
    wait_until_blocked,
)

from app.core.json_types import uuid_from_row
from app.dao.gateway_report_delivery_dao import gateway_report_delivery_dao
from app.dao.personal_burst_dao import personal_burst_dao
from app.db.session import transaction
from app.services.channels.personal_burst_outcome import PersonalAccepted, PersonalReplayed
from app.services.channels.submissions import AcceptedChannelEvent, ChannelEventConflictError, accept_channel_event

__all__ = ["channel_db", "linked", "receipt_db"]


async def test_first_original_is_admitted_with_individual_records_and_deferred_routing(linked: LinkedBurst):
    # Given a linked Personal socket with quota for the message.
    # When its first self text is accepted.
    result = await submit(linked, "n1", "first")

    # Then one pending reserved-marker anchor holds the raw text without any model selection.
    anchor = (await rows(linked, "SELECT * FROM gateway_messages"))[0]
    assert (anchor["status"], anchor["conversation_id"], anchor["content"]) == ("pending", str(linked.shared), "first")
    assert (anchor["routing_reason"], anchor["selected_slot"], anchor["guest_model_ref"]) == (
        "whatsapp_burst",
        None,
        None,
    )
    assert result.personal == PersonalAccepted(gateway_id=uuid_from_row(anchor["id"]), ordinal=1, started_batch=True)
    # And the receipt, transcript row, member link, single charge and reply intent each exist once.
    member = (await rows(linked, "SELECT * FROM personal_burst_inputs"))[0]
    transcript = await rows(linked, "SELECT id, content, conversation_id FROM chat_messages WHERE role = 'user'")
    assert [(r["content"], r["conversation_id"]) for r in transcript] == [("first", str(linked.shared))]
    assert (member["outcome"], member["ordinal"], member["text"], member["chat_message_id"]) == (
        "accepted",
        1,
        "first",
        transcript[0]["id"],
    )
    assert await scalar(linked, "SELECT count(*) FROM channel_event_receipts") == 1
    assert (
        await scalar(linked, "SELECT quota_messages_used FROM users WHERE id = %(id)s", {"id": linked.f.db.user.id})
        == 1
    )
    assert await scalar(linked, "SELECT count(*) FROM gateway_report_deliveries WHERE destination_kind = 'im'") == 1
    window = await scalar(linked, "SELECT EXTRACT(EPOCH FROM ready_at - first_at)::float8 FROM personal_burst_batches")
    assert window == LIMITS.quiet_seconds


async def test_burst_of_distinct_and_equal_texts_joins_one_anchor_with_one_charge_each(linked: LinkedBurst):
    # Given three quick self texts, two with identical content.
    # When they are accepted one after another.
    results = [await submit(linked, key, text) for key, text in (("n1", "same"), ("n2", "other"), ("n3", "same"))]

    # Then they share the first anchor, in order, and equal text is never deduplicated.
    gateway_id = await only_batch(linked)
    assert [r.personal for r in results] == [
        PersonalAccepted(gateway_id, 1, True),
        PersonalAccepted(gateway_id, 2, False),
        PersonalAccepted(gateway_id, 3, False),
    ]
    members = await rows(linked, "SELECT ordinal, text FROM personal_burst_inputs ORDER BY ordinal")
    assert [(m["ordinal"], m["text"]) for m in members] == [(1, "same"), (2, "other"), (3, "same")]
    assert await scalar(linked, "SELECT count(*) FROM gateway_messages") == 1
    assert await scalar(linked, "SELECT count(*) FROM chat_messages WHERE role = 'user'") == 3
    assert (
        await scalar(linked, "SELECT quota_messages_used FROM users WHERE id = %(id)s", {"id": linked.f.db.user.id})
        == 3
    )
    assert await scalar(linked, "SELECT count(*) FROM gateway_report_deliveries") == 1


async def test_replay_adds_no_member_charge_or_timer_extension(linked: LinkedBurst):
    # Given an accepted original and its recorded collection window.
    _ = await submit(linked, "n1", "first")
    before = await rows(linked, "SELECT ready_at, last_at, member_count FROM personal_burst_batches")

    # When the same native event is replayed.
    replay = await submit(linked, "n1", "first")

    # Then it reports a stored acceptance and changes nothing.
    assert replay.replayed
    assert replay.personal == PersonalReplayed(stored="accepted")
    assert await rows(linked, "SELECT ready_at, last_at, member_count FROM personal_burst_batches") == before
    assert (
        await scalar(linked, "SELECT quota_messages_used FROM users WHERE id = %(id)s", {"id": linked.f.db.user.id})
        == 1
    )
    assert await scalar(linked, "SELECT count(*) FROM chat_messages WHERE role = 'user'") == 1


async def test_original_after_the_collection_window_starts_a_new_anchor(linked: LinkedBurst):
    # Given a batch whose collection window has elapsed.
    _ = await submit(linked, "n1", "first")
    await age_batch(linked, 10)

    # When another original arrives.
    result = await submit(linked, "n2", "second")

    # Then it never joins the frozen-by-time batch and queues behind it.
    assert isinstance(result.personal, PersonalAccepted)
    assert result.personal.started_batch
    ordered = await rows(linked, "SELECT content FROM gateway_messages ORDER BY created_at, id")
    assert [r["content"] for r in ordered] == ["first", "second"]


async def test_intervening_web_anchor_is_a_batch_barrier(linked: LinkedBurst):
    # Given a collecting batch followed by a web turn on the same shared lane.
    _ = await submit(linked, "n1", "first")
    async with transaction(linked.f.db.first):
        at = await personal_burst_dao.next_created_at(linked.f.db.agent.id, linked.shared)
        await linked.f.db.first.execute(
            "INSERT INTO gateway_messages (id, agent_id, sender_user_id, conversation_id, content, status, created_at) "
            "VALUES (%(id)s, %(agent)s, %(user)s, %(conv)s, 'web turn', 'pending', %(at)s)",
            {
                "id": uuid4(),
                "agent": linked.f.db.agent.id,
                "user": linked.f.db.user.id,
                "conv": str(linked.shared),
                "at": at,
            },
        )

    # When a later original arrives while the first window is still open.
    result = await submit(linked, "n2", "second")

    # Then it starts its own anchor after the web row instead of jumping the queue.
    assert isinstance(result.personal, PersonalAccepted)
    assert result.personal.started_batch
    ordered = await rows(linked, "SELECT content FROM gateway_messages ORDER BY created_at, id")
    assert [r["content"] for r in ordered] == ["first", "web turn", "second"]
    assert await scalar(linked, "SELECT member_count FROM personal_burst_batches ORDER BY created_at LIMIT 1") == 1


async def test_original_that_does_not_fit_the_batch_body_starts_a_new_batch(linked: LinkedBurst, monkeypatch):
    # Given a body limit that fits one ten-byte original plus a second only without its separator.
    use_limits(monkeypatch, replace(LIMITS, batch_bytes=21, pending_bytes=100))
    _ = await submit(linked, "n1", "a" * 10)

    # When a second ten-byte original would need 22 bytes joined.
    result = await submit(linked, "n2", "b" * 10)

    # Then the separator counts toward the body, so it starts a new batch.
    assert isinstance(result.personal, PersonalAccepted)
    assert result.personal.started_batch
    assert await scalar(linked, "SELECT count(*) FROM personal_burst_batches") == 2


async def test_admin_sender_is_admitted_without_a_quota_charge(linked: LinkedBurst):
    # Given an organization admin whose quota is zero.
    async with transaction(linked.f.db.first):
        await linked.f.db.first.execute(
            "UPDATE users SET role = 'org_admin', quota_message_limit = 0 WHERE id = %(id)s",
            {"id": linked.f.db.user.id},
        )

    # When the admin's self text is accepted.
    result = await submit(linked, "n1", "admin")

    # Then it is admitted and nothing is charged.
    assert isinstance(result.personal, PersonalAccepted)
    assert (
        await scalar(linked, "SELECT quota_messages_used FROM users WHERE id = %(id)s", {"id": linked.f.db.user.id})
        == 0
    )


async def test_concurrent_originals_serialize_behind_the_shared_lane_lock(linked: LinkedBurst):
    # Given an open batch and a competing transaction holding the shared chat-session lock that claiming also uses.
    _ = await submit(linked, "n0", "first")
    held, release, done = anyio.Event(), anyio.Event(), anyio.Event()
    first_db, second_db = linked.f.db.first, linked.f.db.second
    result: list[AcceptedChannelEvent] = []

    async def contender() -> None:
        async with transaction(first_db):
            await first_db.execute("SELECT id FROM chat_sessions WHERE id = %(id)s FOR UPDATE", {"id": linked.shared})
            held.set()
            await release.wait()

    async def admit() -> None:
        await held.wait()
        async with transaction(second_db):
            result.append(await accept_channel_event(event_for(linked.owner, "n1", "second")))
        done.set()

    async def unblock() -> None:
        await held.wait()
        await wait_until_blocked(linked)
        assert not done.is_set()
        release.set()

    # When an original is admitted concurrently.
    async with anyio.create_task_group() as group:
        group.start_soon(contender)
        group.start_soon(admit)
        group.start_soon(unblock)

    # Then admission only completed after the lock was released, and it joined the open batch.
    assert done.is_set()
    assert [r.personal for r in result] == [PersonalAccepted(await only_batch(linked), 2, False)]


async def test_failure_after_the_charge_rolls_back_every_admission_write(linked: LinkedBurst, monkeypatch):
    # Given the reply intent failing after receipt, quota and transcript writes.
    async def explode(**_kwargs: object) -> None:
        raise RuntimeError("intent write failed")

    monkeypatch.setattr(gateway_report_delivery_dao, "create_intent", explode)

    # When the original is accepted.
    with pytest.raises(RuntimeError):
        _ = await submit(linked, "n1", "first")

    # Then nothing durable remains, so the native event can be retried.
    for count in (
        "SELECT count(*) FROM channel_event_receipts",
        "SELECT count(*) FROM gateway_messages",
        "SELECT count(*) FROM personal_burst_batches",
        "SELECT count(*) FROM personal_burst_inputs",
    ):
        assert await scalar(linked, count) == 0
    assert await scalar(linked, "SELECT count(*) FROM chat_messages WHERE role = 'user'") == 0
    assert (
        await scalar(linked, "SELECT quota_messages_used FROM users WHERE id = %(id)s", {"id": linked.f.db.user.id})
        == 0
    )


async def test_route_that_disagrees_with_the_socket_fences_is_denied_without_a_receipt(linked: LinkedBurst):
    # Given a self text whose reply route names another generation than the socket that delivered it.
    event = event_for(linked.owner, "n1", "first")
    stale = replace(event, conversation_id=event.conversation_id.replace(linked.owner.generation.hex, uuid4().hex))

    # When it reaches admission.
    with pytest.raises(HTTPException) as denied:
        async with transaction(linked.f.db.first):
            _ = await accept_channel_event(stale)

    # Then it is refused and leaves no receipt, charge or queue row.
    assert denied.value.status_code == 403
    assert await scalar(linked, "SELECT count(*) FROM channel_event_receipts") == 0


async def test_reused_native_id_with_different_text_conflicts_and_changes_nothing(linked: LinkedBurst):
    # Given an accepted native event.
    _ = await submit(linked, "n1", "first")

    # When the same native ID arrives with different text.
    with pytest.raises(ChannelEventConflictError):
        _ = await submit(linked, "n1", "different")

    # Then the accepted batch and its charge are untouched.
    assert await scalar(linked, "SELECT text FROM personal_burst_inputs") == "first"
    assert (
        await scalar(linked, "SELECT quota_messages_used FROM users WHERE id = %(id)s", {"id": linked.f.db.user.id})
        == 1
    )
