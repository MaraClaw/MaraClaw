import asyncio
from collections.abc import AsyncIterator

import pytest
from tests.burst_dispatch_fixtures import (
    Lane,
    channel_db,
    claim,
    lane,
    legacy_row,
    linked,
    model_named,
    receipt_db,
    transcript,
)

from app.dao.agent_dao import agent_dao
from app.dao.gateway_message_dao import gateway_message_dao
from app.db.session import connection_ctx
from app.records.gateway_message import GatewayMessageRecord
from app.records.openclaw_dispatch import SharedBlock, SharedDispatchRequest
from app.services import openclaw_routing
from app.services.agent_manager import guest_model_ref
from app.services.openclaw_shared_dispatch import claim_shared_turn

__all__ = ["channel_db", "lane", "linked", "receipt_db"]
# receipt_db silences the dispatcher; the retry-storm test needs the real one.
schedule_inbox_wake = openclaw_routing._schedule_inbox_wake


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


@pytest.fixture
async def wakes() -> AsyncIterator[None]:
    openclaw_routing.open_wake_admission()
    try:
        yield None
    finally:
        await openclaw_routing.drain_wake_tasks(0)
        openclaw_routing.open_wake_admission()


async def _join_wakes() -> None:
    while openclaw_routing._wake_tasks:
        await asyncio.wait_for(asyncio.gather(*openclaw_routing._wake_tasks), 5)


async def test_model_unavailable_blocks_the_canonical_lane_without_a_retry_storm(
    lane: Lane, wakes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a canonical lane whose head is pinned to a model the pool no longer has, and a routable turn behind it.
    head = await legacy_row(lane, "pinned", model_ref=guest_model_ref(model_named("retired")))
    behind = await legacy_row(lane, "routable", model_ref=guest_model_ref(lane.guest.bundle.primary))
    agent = await agent_dao.get(lane.agent_id)
    assert agent is not None
    attempts: list[tuple[SharedBlock | None, SharedDispatchRequest]] = []

    async def observed(request: SharedDispatchRequest) -> GatewayMessageRecord | None:
        result = await claim_shared_turn(request)
        attempts.append((result.blocked, request))
        return result.message

    monkeypatch.setattr(openclaw_routing, "claim_ready_shared", observed)
    monkeypatch.setattr(openclaw_routing, "_schedule_inbox_wake", schedule_inbox_wake)
    # When the real dispatcher is woken for the lane twice.
    for message_id in (head, behind):
        openclaw_routing.wake_shared_lane(agent, message_id)
        await _join_wakes()
    # Then each wake makes exactly one human-lane attempt, which stays blocked on the head without touching the guest.
    assert [blocked for blocked, _request in attempts] == ["model_unavailable", "model_unavailable"]
    assert {request.session_id for _blocked, request in attempts} == {await transcript(lane)}
    assert lane.guest.written == []
    async with connection_ctx() as db:
        statuses = await db.fetchall(
            "SELECT id, status FROM gateway_messages WHERE agent_id = %(agent)s ORDER BY created_at",
            {"agent": lane.agent_id},
        )
    assert [(row["id"], row["status"]) for row in statuses] == [(head, "pending"), (behind, "pending")]
