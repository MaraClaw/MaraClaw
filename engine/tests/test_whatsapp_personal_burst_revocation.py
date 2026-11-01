from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from tests.personal_burst_fixtures import (
    LinkedBurst,
    age_batch,
    channel_db,
    linked,
    only_batch,
    receipt_db,
    rows,
    scalar,
    submit,
)

from app.dao.personal_burst_dispatch_dao import personal_burst_dispatch_dao
from app.dao.whatsapp_personal_dao import claim, get_personal, reaped, revoke
from app.db.session import transaction
from app.records.personal_burst import BURST_CANCELLED_RESULT
from app.records.whatsapp_personal import PersonalConnection
from app.services.channels.whatsapp_personal_control import start
from app.services.channels.whatsapp_personal_events import handle_frame
from app.services.channels.whatsapp_personal_protocol import Identity

__all__ = ["channel_db", "linked", "receipt_db"]


class _AbortError(Exception):
    pass


async def _state(linked: LinkedBurst, gateway_id: UUID) -> tuple[object, object, object, object]:
    batch = (
        await rows(
            linked, "SELECT state, cancel_reason FROM personal_burst_batches WHERE gateway_id=%(g)s", {"g": gateway_id}
        )
    )[0]
    gateway = (
        await rows(linked, "SELECT status, conversation_id FROM gateway_messages WHERE id=%(g)s", {"g": gateway_id})
    )[0]
    return batch["state"], batch["cancel_reason"], gateway["status"], gateway["conversation_id"]


async def _reconnect(linked: LinkedBurst) -> PersonalConnection:
    """Relink a revoked connection through the real control/claim/identity path (a fresh generation)."""
    async with transaction(linked.f.db.first):
        stale = await get_personal(linked.owner.agent_id)
        assert stale is not None
        await start(linked.owner.agent_id, linked.f.db.user.id, stale.generation)
        owner = await claim(linked.owner.config_id)
        assert owner is not None
        account = "15550001@s.whatsapp.net"
        await handle_frame(
            owner, Identity(type="identity", id="id", token="test", account_id=account, pn_jid=account, lid_jid=None)
        )
        fresh = await get_personal(linked.owner.agent_id)
        assert fresh is not None
        return fresh


async def _revoke_then_fail(linked: LinkedBurst) -> None:
    async with transaction(linked.f.db.first):
        await revoke(linked.owner.agent_id, purge=False)
        raise _AbortError


async def _revoke(linked: LinkedBurst, *, purge: bool = False) -> None:
    async with transaction(linked.f.db.first):
        await revoke(linked.owner.agent_id, purge=purge)


async def test_revoke_cancels_pending_batch_with_fence_and_intent_cleanup_but_keeps_accepted_work(linked: LinkedBurst):
    # Given two accepted originals collecting in one pending batch, plus an unrelated pending web row in the lane.
    await submit(linked, "n1", "one")
    await submit(linked, "n2", "two")
    gateway_id = await only_batch(linked)
    web = uuid4()
    async with transaction(linked.f.db.first):
        await linked.f.db.first.execute(
            "INSERT INTO gateway_messages (id, agent_id, sender_user_id, conversation_id, content, status) "
            "VALUES (%(id)s, %(agent)s, %(user)s, %(conv)s, 'web', 'pending')",
            {"id": web, "agent": linked.owner.agent_id, "user": linked.f.db.user.id, "conv": str(linked.shared)},
        )

    # When the connection is revoked and the transaction commits.
    await _revoke(linked)

    # Then the batch is cancelled with its queue row completed and detached from the shared lane.
    assert await _state(linked, gateway_id) == ("cancelled", "disconnected", "completed", None)
    assert (
        await scalar(linked, "SELECT result FROM gateway_messages WHERE id=%(g)s", {"g": gateway_id})
        == BURST_CANCELLED_RESULT
    )
    # And its undelivered source intent is gone.
    assert (
        await scalar(linked, "SELECT count(*) FROM gateway_report_deliveries WHERE message_id=%(g)s", {"g": gateway_id})
        == 0
    )
    # And accepted originals, transcript rows, receipts and quota are all retained (no refund).
    assert await scalar(linked, "SELECT count(*) FROM personal_burst_inputs WHERE outcome='accepted'") == 2
    assert await scalar(linked, "SELECT count(*) FROM chat_messages WHERE role='user'") == 2
    assert await scalar(linked, "SELECT count(*) FROM channel_event_receipts") == 2
    assert (
        await scalar(linked, "SELECT quota_messages_used FROM users WHERE id=%(id)s", {"id": linked.f.db.user.id}) == 2
    )
    # And the unrelated web row stays pending.
    assert await scalar(linked, "SELECT status FROM gateway_messages WHERE id=%(g)s", {"g": web}) == "pending"


async def test_revoke_cancels_a_sealed_batch(linked: LinkedBurst):
    # Given a due batch already frozen but not yet claimed.
    await submit(linked, "n1", "one")
    gateway_id = await only_batch(linked)
    await age_batch(linked, 10)
    async with transaction(linked.f.db.first):
        sealed = await personal_burst_dispatch_dao.seal(gateway_id)
    assert sealed is not None
    assert sealed.state == "sealed"

    # When the connection is purged.
    await _revoke(linked, purge=True)

    # Then the sealed batch is cancelled the same way.
    assert await _state(linked, gateway_id) == ("cancelled", "disconnected", "completed", None)


async def test_revoke_rollback_restores_both_the_link_and_the_batch(linked: LinkedBurst):
    # Given a pending batch and a connected link.
    await submit(linked, "n1", "one")
    gateway_id = await only_batch(linked)
    before = (await rows(linked, "SELECT desired, generation FROM whatsapp_personal_connections"))[0]

    # When the outer transaction revokes and then fails.
    with pytest.raises(_AbortError):
        await _revoke_then_fail(linked)

    # Then neither the link invalidation nor the cancellation survives.
    after = (await rows(linked, "SELECT desired, generation FROM whatsapp_personal_connections"))[0]
    assert (after["desired"], after["generation"]) == (before["desired"], before["generation"])
    assert await _state(linked, gateway_id) == ("pending", None, "pending", str(linked.shared))


async def test_revoke_leaves_a_claimed_uncertain_turn_untouched(linked: LinkedBurst):
    # Given a batch whose gateway turn was already claimed and delivered to the guest.
    await submit(linked, "n1", "one")
    gateway_id = await only_batch(linked)
    await age_batch(linked, 10)
    async with transaction(linked.f.db.first):
        assert await personal_burst_dispatch_dao.seal(gateway_id) is not None
        assert await personal_burst_dispatch_dao.mark_claimed(gateway_id)
        await linked.f.db.first.execute(
            "UPDATE gateway_messages SET status='delivered' WHERE id=%(g)s", {"g": gateway_id}
        )

    # When the connection is revoked.
    await _revoke(linked, purge=True)

    # Then the claimed batch and delivered turn are not reset, requeued or cancelled.
    assert await _state(linked, gateway_id) == ("claimed", None, "delivered", str(linked.shared))


async def test_fresh_generation_accepts_a_new_batch_after_revocation(linked: LinkedBurst):
    # Given a revoked and reaped link whose pending batch was cancelled.
    await submit(linked, "n1", "old")
    old = await only_batch(linked)
    await _revoke(linked, purge=True)
    async with transaction(linked.f.db.first):
        assert await reaped(linked.owner)

    # When the user reconnects with a new generation and sends again.
    owner = await _reconnect(linked)
    fresh = replace(linked, owner=owner)
    assert owner.generation != linked.owner.generation
    await submit(fresh, "n2", "new")

    # Then a new pending batch exists next to the cancelled one.
    states = await rows(linked, "SELECT gateway_id, state FROM personal_burst_batches ORDER BY created_at")
    assert [(r["gateway_id"], r["state"]) for r in states] == [(old, "cancelled"), (states[1]["gateway_id"], "pending")]
    assert states[1]["gateway_id"] != old


async def test_revoke_is_safe_and_idempotent_without_open_batches_or_a_connection_row(linked: LinkedBurst):
    # Given a link with no admitted work.
    # When it is revoked twice and an unknown agent is revoked.
    await _revoke(linked)
    await _revoke(linked, purge=True)
    async with transaction(linked.f.db.first):
        await revoke(uuid4(), purge=False)

    # Then nothing was created or cancelled.
    assert await scalar(linked, "SELECT count(*) FROM personal_burst_batches") == 0
