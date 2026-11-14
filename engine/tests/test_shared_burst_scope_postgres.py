from uuid import uuid4

import pytest
from tests.burst_dispatch_fixtures import (
    Lane,
    batch_state,
    channel_db,
    claim,
    gateway,
    lane,
    legacy_row,
    linked,
    model_named,
    receipt_db,
    update_connection,
)
from tests.personal_burst_fixtures import age_batch, only_batch, scalar, submit

from app.db.session import transaction
from app.records.personal_burst import BURST_CANCELLED_RESULT
from app.services.agent_manager import guest_model_ref

__all__ = ["channel_db", "lane", "linked", "receipt_db"]

FENCES = {
    "auth_epoch": "UPDATE whatsapp_personal_connections SET auth_epoch = gen_random_uuid() WHERE agent_id = %(id)s",
    "generation": "UPDATE whatsapp_personal_connections SET generation = gen_random_uuid() WHERE agent_id = %(id)s",
    "account": "UPDATE whatsapp_personal_connections SET account_id = 'another-account' WHERE agent_id = %(id)s",
    "peer": "UPDATE whatsapp_personal_connections SET pn_jid = '9@s.whatsapp.net', lid_jid = '9@lid' WHERE agent_id = %(id)s",
    "owner": "UPDATE whatsapp_personal_connections SET owner_id = (SELECT id FROM users WHERE id <> owner_id AND tenant_id IS NOT NULL LIMIT 1) WHERE agent_id = %(id)s",
    "tenant": "UPDATE whatsapp_personal_connections SET tenant_id = (SELECT id FROM tenants WHERE id <> tenant_id LIMIT 1) WHERE agent_id = %(id)s",
    "disconnected": "UPDATE whatsapp_personal_connections SET desired = FALSE WHERE agent_id = %(id)s",
    "failed": "UPDATE whatsapp_personal_connections SET state = 'failed' WHERE agent_id = %(id)s",
}
RECONNECTING = (
    "UPDATE whatsapp_personal_connections SET state = 'reconnecting', claim_token = NULL, lease_until = NULL "
    "WHERE agent_id = %(id)s"
)
CONNECTED = "UPDATE whatsapp_personal_connections SET state = 'connected' WHERE agent_id = %(id)s"


async def due_pair(lane: Lane):
    _ = await submit(lane.linked, "n0", "one")
    _ = await submit(lane.linked, "n1", "two")
    await age_batch(lane.linked, 10)
    return await only_batch(lane.linked)


async def assert_cancelled_with_work_retained(lane: Lane, gateway_id) -> None:
    row = await gateway(lane, gateway_id)
    assert (row.status, row.conversation_id, row.result) == ("completed", None, BURST_CANCELLED_RESULT)
    assert await batch_state(lane, gateway_id) == "cancelled"
    assert await scalar(lane.linked, "SELECT count(*) FROM chat_messages WHERE role = 'user'") == 2
    assert (
        await scalar(
            lane.linked, "SELECT quota_messages_used FROM users WHERE id = %(u)s", {"u": lane.linked.f.db.user.id}
        )
        == 2
    )
    assert lane.guest.written == []


@pytest.mark.parametrize("fence", FENCES)
async def test_a_changed_fence_cancels_the_due_batch_but_keeps_transcripts_and_quota(lane: Lane, fence: str) -> None:
    # Given a due batch and a fence that no longer matches its frozen scope.
    gateway_id = await due_pair(lane)
    if fence == "tenant":
        async with transaction(lane.linked.f.db.first):
            await lane.linked.f.db.first.execute(
                "INSERT INTO tenants (id, name, slug) VALUES (%(id)s, 'x', 'x')", {"id": uuid4()}
            )
    # When dispatch is attempted.
    await update_connection(lane, FENCES[fence])
    result = await claim(lane)
    # Then the batch is cancelled, nothing runs on the old route, and accepted work is retained.
    assert result.message is None
    await assert_cancelled_with_work_retained(lane, gateway_id)


async def test_a_stale_reply_route_cancels_the_due_batch(lane: Lane) -> None:
    # Given a due batch whose reply conversation no longer names its route.
    gateway_id = await due_pair(lane)
    async with transaction(lane.linked.f.db.first):
        await lane.linked.f.db.first.execute(
            "UPDATE chat_sessions SET external_conv_id = 'whatsapp_other' WHERE source_channel = 'whatsapp'"
        )
    # When dispatch is attempted, it is cancelled with originals retained.
    assert (await claim(lane)).message is None
    await assert_cancelled_with_work_retained(lane, gateway_id)


async def test_a_graceful_restart_defers_the_valid_batch_instead_of_cancelling_it(lane: Lane) -> None:
    # Given a due batch and a socket reaped for a process restart (same owner, epoch, generation, account, peer).
    gateway_id = await due_pair(lane)
    await update_connection(lane, RECONNECTING)
    # When dispatch is attempted before the socket is back.
    deferred = await claim(lane)
    # Then the turn is kept pending with all members, and runs once the same connection is live again.
    assert (deferred.message, deferred.blocked) == (None, "reconnecting")
    assert (await gateway(lane, gateway_id)).status == "pending"
    assert await batch_state(lane, gateway_id) == "pending"
    assert (
        await scalar(
            lane.linked, "SELECT count(*) FROM personal_burst_inputs WHERE batch_id = %(i)s", {"i": gateway_id}
        )
        == 2
    )
    await update_connection(lane, CONNECTED)
    resumed = (await claim(lane)).message
    assert resumed is not None
    assert resumed.content == "one\n\ntwo"


async def test_a_missing_header_fails_closed_instead_of_passing_as_a_legacy_row(lane: Lane) -> None:
    # Given a reserved-marker anchor whose header was removed.
    gateway_id = await due_pair(lane)
    async with transaction(lane.linked.f.db.first):
        await lane.linked.f.db.first.execute(
            "DELETE FROM personal_burst_batches WHERE gateway_id = %(i)s", {"i": gateway_id}
        )
    # When dispatch is attempted.
    assert (await claim(lane)).message is None
    # Then it is never delivered; the anchor ends as cancelled and no config is written.
    row = await gateway(lane, gateway_id)
    assert (row.status, row.result) == ("completed", BURST_CANCELLED_RESULT)
    assert lane.guest.written == []


async def test_a_lost_member_cancels_the_batch_instead_of_dispatching_a_partial_body(lane: Lane) -> None:
    # Given a due batch missing one original (its receipt was removed).
    gateway_id = await due_pair(lane)
    async with transaction(lane.linked.f.db.first):
        await lane.linked.f.db.first.execute(
            "DELETE FROM personal_burst_inputs WHERE batch_id = %(i)s AND ordinal = 2", {"i": gateway_id}
        )
    # When dispatch is attempted, it is cancelled and nothing runs.
    assert (await claim(lane)).message is None
    assert await batch_state(lane, gateway_id) == "cancelled"
    assert (await gateway(lane, gateway_id)).status == "completed"
    assert lane.guest.written == []


async def test_a_stored_model_that_is_no_longer_assigned_is_never_substituted(lane: Lane) -> None:
    # Given a queued row that names a model the company no longer has.
    gone = guest_model_ref(model_named("retired"))
    queued = await legacy_row(lane, "web", model_ref=gone)
    # When dispatch is attempted.
    blocked = await claim(lane)
    # Then it stays pending with no guest write; once the model is assigned again it runs on exactly that model.
    assert (blocked.message, blocked.blocked) == (None, "model_unavailable")
    assert (await gateway(lane, queued)).status == "pending"
    assert lane.guest.written == []
    lane.guest.bundle.fallback = model_named("retired")
    claimed = (await claim(lane)).message
    assert claimed is not None
    assert claimed.id == queued
    assert lane.guest.written == [gone]
