"""The persisted checkpoint holds only while the compared value is current."""

import asyncio
from uuid import uuid4

import test_shared_gateway_postgres as shared_fixtures
from app.dao.wecom_kf_cursor_dao import WeComKFCursorKey, wecom_kf_cursor_dao
from app.db.session import transaction
from test_chat_submission_postgres import ReceiptDatabase

receipt_database = shared_fixtures.receipt_database
pooled_database = shared_fixtures.pooled_database


async def test_kf_cursor_is_fenced_scoped_expiring_and_removed_with_config(pooled_database: ReceiptDatabase) -> None:
    db = pooled_database
    config_id = uuid4()
    async with transaction(db.first):
        await db.first.execute(
            "INSERT INTO channel_configs (id, agent_id, channel_type, is_configured, is_connected) "
            "VALUES (%(id)s, %(agent)s, 'wecom', true, false)",
            {"id": config_id, "agent": db.agent.id},
        )
    key = WeComKFCursorKey(config_id, "corp", "")
    assert await wecom_kf_cursor_dao.read(key) is None
    assert not await wecom_kf_cursor_dao.compare_and_set(key, "not-present", "ignored")
    tokens = ("first", "second")
    settled = await asyncio.gather(*(wecom_kf_cursor_dao.compare_and_set(key, None, item) for item in tokens))
    assert sorted(settled) == [False, True]
    winner = tokens[settled.index(True)]
    assert await wecom_kf_cursor_dao.read(key) == winner
    assert await wecom_kf_cursor_dao.compare_and_set(key, winner, "third")
    assert not await wecom_kf_cursor_dao.compare_and_set(key, winner, "regression")
    assert await wecom_kf_cursor_dao.read(WeComKFCursorKey(config_id, "corp", "other")) is None
    assert await wecom_kf_cursor_dao.read(WeComKFCursorKey(config_id, "other", "")) is None
    async with transaction(db.first):
        await db.first.execute("UPDATE wecom_kf_cursors SET updated_at = now() - INTERVAL '3 days 1 minute'")
    assert await wecom_kf_cursor_dao.read(key) is None
    assert not await wecom_kf_cursor_dao.compare_and_set(key, "third", "old-owner")
    assert await wecom_kf_cursor_dao.compare_and_set(key, None, "fresh")
    assert await wecom_kf_cursor_dao.read(key) == "fresh"
    async with transaction(db.first):
        await db.first.execute("DELETE FROM channel_configs WHERE id = %(id)s", {"id": config_id})
        assert await db.first.fetchval("SELECT count(*) FROM wecom_kf_cursors") == 0
