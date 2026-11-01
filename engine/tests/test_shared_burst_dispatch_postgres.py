import asyncio
from datetime import UTC, datetime
from uuid import UUID

from tests.burst_dispatch_fixtures import (
    Lane,
    age_pending,
    backdate_delivery,
    batch_state,
    channel_db,
    claim,
    gateway,
    lane,
    legacy_row,
    linked,
    model_named,
    receipt_db,
    report,
)
from tests.personal_burst_fixtures import age_batch, only_batch, scalar, submit

from app.records.openclaw_dispatch import SharedDispatchRequest
from app.services import openclaw_routing
from app.services.agent_manager import guest_model_ref
from app.services.openclaw_gateway_delivery import claim_poll_batch
from app.services.openclaw_shared_dispatch import claim_ready_shared

__all__ = ["channel_db", "lane", "linked", "receipt_db"]


async def due_batch(lane: Lane, *texts: str) -> UUID:
    for index, text in enumerate(texts):
        _ = await submit(lane.linked, f"n{index}", text)
    gateway_id = await only_batch(lane.linked)
    await age_batch(lane.linked, 10)
    return gateway_id


async def test_due_batch_is_claimed_once_as_its_joined_body_with_the_routed_model(lane: Lane) -> None:
    # Given three self messages collected into one due logical turn.
    gateway_id = await due_batch(lane, "first", "second", "third")
    # When the central claim runs.
    result = await claim(lane)
    # Then it is delivered as the ordered joined body, routed at dispatch, batch claimed, model applied once.
    row = result.message
    assert row is not None
    assert row.id == gateway_id
    assert (row.status, row.content) == ("delivered", "first\n\nsecond\n\nthird")
    assert (row.selected_slot, row.guest_model_ref) == ("primary", guest_model_ref(lane.guest.bundle.primary))
    assert row.routing_reason != "whatsapp_burst"
    assert await batch_state(lane, gateway_id) == "claimed"
    assert lane.guest.written == [guest_model_ref(lane.guest.bundle.primary)]


async def test_an_unready_head_blocks_every_younger_row_and_changes_nothing(lane: Lane) -> None:
    # Given a collecting Personal batch followed by a web row.
    _ = await submit(lane.linked, "n0", "still typing")
    gateway_id = await only_batch(lane.linked)
    web = await legacy_row(lane, "web")
    # When both the central claim and the guest poll try before the quiet period ends.
    blocked = await claim(lane)
    polled = await claim_poll_batch(lane.agent_id)
    # Then nothing is delivered, the web row is not skipped ahead, and no guest config was written.
    assert (blocked.message, blocked.blocked, polled) == (None, "not_due", [])
    assert [(await gateway(lane, i)).status for i in (gateway_id, web)] == ["pending", "pending"]
    assert lane.guest.written == []


async def test_wake_and_poll_use_the_same_gate_and_race_to_one_winner(lane: Lane) -> None:
    # Given one due batch and independent callers: two polls, two wakes, two direct claims.
    gateway_id = await due_batch(lane, "a", "b")
    now = datetime.now(UTC)
    # When they all race.
    outcomes = await asyncio.gather(
        claim_poll_batch(lane.agent_id),
        claim_poll_batch(lane.agent_id),
        openclaw_routing._claim_wake(lane.agent_id, gateway_id),
        openclaw_routing._claim_wake(lane.agent_id, gateway_id),
        claim_ready_shared(SharedDispatchRequest(lane.agent_id, now)),
        claim_ready_shared(SharedDispatchRequest(lane.agent_id, now)),
    )
    # Then exactly one claimed the turn and the guest model was written once.
    claimed = [row for out in outcomes for row in (out if isinstance(out, list) else [out]) if row is not None]
    assert [row.id for row in claimed] == [gateway_id]
    assert lane.guest.written == [guest_model_ref(lane.guest.bundle.primary)]


async def test_an_active_uncertain_turn_blocks_the_lane_and_is_never_requeued(lane: Lane) -> None:
    # Given a claimed turn whose delivery is hours old and was never reported (uncertain).
    gateway_id = await due_batch(lane, "first")
    assert (await claim(lane)).message is not None
    await backdate_delivery(lane, 3)
    _ = await submit(lane.linked, "n9", "next")
    await age_pending(lane, 10)
    # When the guest polls and a wake arrives.
    polled = await claim_poll_batch(lane.agent_id)
    blocked = await claim(lane)
    # Then the old turn stays delivered, the new batch waits, and no model is applied twice.
    assert polled == []
    assert blocked.blocked == "active"
    assert (await gateway(lane, gateway_id)).status == "delivered"
    assert len(lane.guest.written) == 1


async def test_claimed_membership_and_model_are_stable_while_new_originals_wait(lane: Lane) -> None:
    # Given a claimed batch running on the first model.
    first = await due_batch(lane, "one", "two")
    claimed = (await claim(lane)).message
    assert claimed is not None
    # When a later original arrives and the model pool changes.
    _ = await submit(lane.linked, "late", "three")
    lane.guest.bundle.primary = model_named("sonnet")
    await age_pending(lane, 10)
    # Then it is a separate pending turn, the running body is unchanged and nothing was rewritten.
    assert (await gateway(lane, first)).content == "one\n\ntwo"
    assert await scalar(lane.linked, "SELECT count(*) FROM personal_burst_batches") == 2
    assert (await claim(lane)).blocked == "active"
    assert lane.guest.written == [guest_model_ref(model_named("opus"))]


async def test_first_report_completes_the_batch_and_the_next_turn_runs_on_its_own_model(lane: Lane) -> None:
    # Given a running batch and a queued web row stored on a different model.
    first = await due_batch(lane, "one")
    assert (await claim(lane)).message is not None
    web = await legacy_row(lane, "web", model_ref=guest_model_ref(lane.guest.bundle.primary))
    # When the first report wins and a second one repeats.
    winning = await report(lane, first, "answer")
    repeat = await report(lane, first, "other")
    nxt = await claim(lane)
    # Then the batch is completed with the first result and the web row is the next claim.
    assert winning is not None
    assert repeat is None
    assert (await batch_state(lane, first), (await gateway(lane, first)).result) == ("completed", "answer")
    assert nxt.message is not None
    assert nxt.message.id == web


async def test_a_web_row_between_originals_is_a_barrier_and_turns_run_in_fifo_order(lane: Lane) -> None:
    # Given a due batch, a web row, then a later original that must not join the earlier batch.
    first = await due_batch(lane, "a")
    web = await legacy_row(lane, "web", model_ref=guest_model_ref(lane.guest.bundle.primary))
    _ = await submit(lane.linked, "late", "b")
    await age_pending(lane, 10)
    later = await scalar(
        lane.linked, "SELECT gateway_id FROM personal_burst_batches WHERE gateway_id <> %(f)s", {"f": first}
    )
    # When the lane is claimed and reported turn by turn.
    order: list[UUID] = []
    for _ in range(3):
        row = (await claim(lane)).message
        assert row is not None
        order.append(row.id)
        assert (await report(lane, row.id)) is not None
    # Then the turns ran strictly in FIFO order, one at a time.
    assert order == [first, web, UUID(str(later))]
