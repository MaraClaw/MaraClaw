from tests.test_whatsapp_personal_burst_reconciler_fixtures import (
    add_lane,
    burst_db,
    deliver,
    due_batch,
    due_lanes,
    occupy,
    receipt_db,
)
from tests.test_whatsapp_personal_burst_store_fixtures import admit, age, anchor, present

from app.dao.personal_burst_dispatch_dao import DueHeadCursor, personal_burst_dispatch_dao as dispatch
from app.db.session import transaction

__all__ = ["burst_db", "receipt_db"]


async def page_ids(f, limit, *, available_only=False, after=None):
    async with transaction(f.db.second):
        page = await dispatch.due_heads_page(limit, available_only=available_only, after=after)
    return [head.gateway_id for head in page.heads], page.next_after


async def test_default_due_heads_still_reports_occupied_lanes_unfiltered(burst_db):
    # Given two due lanes where the older one has an unreported delivered turn.
    f = burst_db
    (first, a), (_, b) = await due_lanes(f, 2)
    _ = await occupy(first)
    # When the legacy API runs, Then both heads are listed and the active flag is set only on the occupied lane.
    async with transaction(f.db.second):
        heads = await dispatch.due_heads(10)
    assert [(h.gateway_id, h.active) for h in heads] == [(a.gateway_id, True), (b.gateway_id, False)]


async def test_available_only_excludes_occupied_lanes_before_the_global_limit(burst_db):
    # Given three older occupied lanes ahead of one available lane.
    f = burst_db
    lanes = await due_lanes(f, 4)
    for lane, _ in lanes[:3]:
        _ = await occupy(lane)
    # When one head is requested, Then the limit is spent on the available lane, not on occupied ones.
    ids, _ = await page_ids(f, 1, available_only=True)
    assert ids == [lanes[3][1].gateway_id]
    # And the legacy listing with the same limit is still filled by the oldest (occupied) lane.
    async with transaction(f.db.second):
        assert [h.gateway_id for h in await dispatch.due_heads(1)] == [lanes[0][1].gateway_id]


async def test_cursor_walks_every_due_head_once_then_reports_the_end(burst_db):
    f = burst_db
    batches = [batch.gateway_id for _, batch in await due_lanes(f, 5)]
    first, cursor = await page_ids(f, 2, available_only=True)
    second, cursor = await page_ids(f, 2, available_only=True, after=cursor)
    third, cursor = await page_ids(f, 2, available_only=True, after=cursor)
    assert (first, second, third) == (batches[:2], batches[2:4], batches[4:])
    assert cursor is None


async def test_cursor_survives_its_own_row_leaving_and_sees_older_work_only_after_a_wrap(burst_db):
    f = burst_db
    lanes = await due_lanes(f, 3)
    first, cursor = await page_ids(f, 1, available_only=True)
    assert first == [lanes[0][1].gateway_id]
    # Given the cursor row is dispatched and an older-than-cursor head appears in a new lane.
    await deliver(lanes[0][0], lanes[0][1].gateway_id)
    newcomer = await add_lane(f)
    fresh = await due_batch(newcomer, "late")
    await age(newcomer, fresh.gateway_id, 100)
    # When paging continues, Then it moves forward without error and the older newcomer waits for the wrap.
    second, cursor = await page_ids(f, 1, available_only=True, after=cursor)
    assert second == [lanes[1][1].gateway_id]
    wrapped, _ = await page_ids(f, 1, available_only=True)
    assert wrapped == [fresh.gateway_id]


async def test_unready_real_head_is_never_bypassed_by_a_due_batch_behind_it(burst_db):
    # Given a collecting (unready) batch, a web row, then a due batch in the same lane.
    f = burst_db
    unready = await admit(f, "n1", "a")
    async with transaction(f.db.first):
        _ = await anchor(f, content="web")
    behind = await due_batch(f, "n2")
    # Then no head is due: the actual oldest pending row is not ready.
    assert await page_ids(f, 10) == ([], None)
    assert await page_ids(f, 10, available_only=True) == ([], None)
    # And once the head is due it is the only listed lane head (the batch behind stays behind the web row).
    await age(f, unready.gateway_id, 10)
    ids, _ = await page_ids(f, 10, available_only=True)
    assert ids == [unready.gateway_id]
    assert behind.gateway_id not in ids


async def test_legacy_head_blocks_a_due_batch_behind_it(burst_db):
    f = burst_db
    async with transaction(f.db.first):
        _ = await anchor(f, content="legacy")
    _ = await due_batch(f)
    assert await page_ids(f, 10, available_only=True) == ([], None)


async def test_page_cursor_is_the_last_returned_row(burst_db):
    f = burst_db
    lanes = await due_lanes(f, 2)
    async with transaction(f.db.second):
        page = await dispatch.due_heads_page(1, available_only=True)
    batch = present(page.heads[0].batch)
    assert page.next_after == DueHeadCursor(batch.ready_at, lanes[0][1].gateway_id)
