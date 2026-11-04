from tests.test_whatsapp_personal_burst_reconciler_fixtures import burst_db, deliver, due_batch, due_lanes, receipt_db
from tests.test_whatsapp_personal_burst_store_fixtures import anchor

from app.dao.gateway_message_dao import gateway_message_dao
from app.db.session import transaction

__all__ = ["burst_db", "receipt_db"]


async def test_native_cursor_continues_after_its_row_leaves_the_pending_queue(burst_db):
    f = burst_db
    lanes = await due_lanes(f, 3)
    async with transaction(f.db.second):
        first, cursor = await gateway_message_dao.pending_dispatch_page(1)
    assert [row.id for row in first] == [lanes[0][1].gateway_id]
    assert cursor is not None
    assert cursor == (first[0].created_at, first[0].id)

    await deliver(lanes[0][0], lanes[0][1].gateway_id)
    async with transaction(f.db.second):
        second, cursor = await gateway_message_dao.pending_dispatch_page(1, after=cursor)
        assert cursor is not None
        third, cursor = await gateway_message_dao.pending_dispatch_page(1, after=cursor)
    assert [row.id for row in second + third] == [lanes[1][1].gateway_id, lanes[2][1].gateway_id]
    assert cursor is None


async def test_native_page_returns_ordinary_head_before_due_batch_in_same_lane(burst_db):
    f = burst_db
    async with transaction(f.db.first):
        ordinary = await anchor(f, content="web")
    behind = await due_batch(f)
    async with transaction(f.db.second):
        rows, cursor = await gateway_message_dao.pending_dispatch_page(10)
    assert [row.id for row in rows] == [ordinary]
    assert behind.gateway_id not in [row.id for row in rows]
    assert cursor is None
