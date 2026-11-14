from tests.burst_dispatch_fixtures import (
    Lane,
    channel_db,
    claim,
    lane,
    legacy_row,
    linked,
    model_named,
    receipt_db,
)

from app.dao.agent_dao import agent_dao
from app.dao.gateway_message_dao import gateway_message_dao
from app.db.session import connection_ctx
from app.services import openclaw_routing
from app.services.agent_manager import guest_model_ref

__all__ = ["channel_db", "lane", "linked", "receipt_db"]


async def test_shared_fallback_selection_is_applied_even_with_nonshared_primary_pending(lane: Lane) -> None:
    # Given an exactly selected Shared model and an unrelated pending primary turn.
    fallback = model_named("haiku")
    lane.guest.bundle.fallback = fallback
    selected_ref = guest_model_ref(fallback)
    shared_id = await legacy_row(lane, "shared work", model_ref=selected_ref)
    async with connection_ctx() as db:
        await db.execute("UPDATE gateway_messages SET selected_slot = 'fallback' WHERE id = %(id)s", {"id": shared_id})
        _ = await gateway_message_dao.create(
            obj_in={
                "agent_id": lane.agent_id,
                "content": "unrelated work",
                "conversation_id": "nonshared-work",
                "selected_slot": "primary",
                "status": "pending",
            }
        )
    # When the Shared head is claimed.
    result = await claim(lane)
    # Then its applied guest configuration matches its stored selection, without substitution.
    assert result.message is not None
    assert result.message.id == shared_id
    assert result.message.guest_model_ref == selected_ref
    assert lane.guest.written == [selected_ref]


async def test_shared_and_nonshared_claims_preserve_independent_models(lane: Lane) -> None:
    # Given a Shared fallback turn alongside an unrelated nonshared primary turn.
    fallback = model_named("haiku")
    lane.guest.bundle.fallback = fallback
    fallback_ref, primary_ref = guest_model_ref(fallback), guest_model_ref(model_named("opus"))
    shared_id = await legacy_row(lane, "shared work", model_ref=fallback_ref)
    async with connection_ctx() as db:
        await db.execute("UPDATE gateway_messages SET selected_slot='fallback' WHERE id=%(id)s", {"id": shared_id})
        nonshared = await gateway_message_dao.create(
            obj_in={
                "agent_id": lane.agent_id,
                "content": "unrelated work",
                "selected_slot": "primary",
                "guest_model_ref": primary_ref,
                "status": "pending",
            }
        )
    # When the Shared lane claims its own head.
    shared = (await claim(lane)).message
    agent = await agent_dao.get(lane.agent_id)
    assert agent is not None
    ordinary = await openclaw_routing._claim_wake(agent, nonshared.id)
    # Then each claimed selection remains independent and each model is applied for its turn.
    assert shared is not None
    assert shared.id == shared_id
    assert (shared.guest_model_ref, shared.selected_slot) == (fallback_ref, "fallback")
    assert ordinary is not None
    assert ordinary.id == nonshared.id
    assert (ordinary.guest_model_ref, ordinary.selected_slot, ordinary.status) == (primary_ref, "primary", "delivered")
    assert lane.guest.written == [fallback_ref, primary_ref]
