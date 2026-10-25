from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.core.notification_kinds import NotificationKind
from app.dao.notification_dao import notification_dao
from app.dao.openclaw_update_dao import OpenClawUpdateNoticeDAO
from app.db import session
from app.db.connection import DbConnection
from app.records.notification import NotificationRecord
from app.services.heartbeat import _notify_oneshot_error


async def test_heartbeat_persists_system_kind_as_plain_string(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    stored = NotificationRecord(id=UUID(int=1), type=NotificationKind.SYSTEM, title="Failed")
    create = AsyncMock(return_value=stored)
    monkeypatch.setattr(notification_dao, "create", create)
    # When
    await _notify_oneshot_error(UUID(int=2), UUID(int=3), "Agent", "Failure")
    # Then
    create.assert_awaited_once()
    assert create.call_args is not None
    fields = create.call_args.kwargs["obj_in"]
    assert fields["type"] == "system"
    assert type(fields["type"]) is str
    assert fields["user_id"] == UUID(int=2)
    assert fields["ref_id"] == UUID(int=3)


async def test_openclaw_fanout_binds_original_kind_string() -> None:
    # Given
    row = {
        "id": UUID(int=1),
        "request_id": UUID(int=2),
        "title": "Update",
        "body": "Details",
        "mandatory": True,
        "recipients": 3,
        "created_at": datetime(2026, 10, 25, tzinfo=UTC),
    }
    db = AsyncMock(spec=DbConnection)
    db.fetchone.return_value = row
    db.fetchval.return_value = 3
    token = session._conn_ctx.set(db)
    try:
        # When
        notice, created = await OpenClawUpdateNoticeDAO().publish(
            request_id=UUID(int=2), title="Update", body="Details", mandatory=True, actor_id=None, sender_name="System"
        )
        # Then
        assert created is True
        assert notice.recipients == 3
        db.fetchval.assert_awaited_once()
        assert db.fetchval.call_args is not None
        query, params = db.fetchval.call_args.args
        assert "%(notification_kind)s" in query
        assert params["notification_kind"] == "openclaw_update"
        assert type(params["notification_kind"]) is str
    finally:
        session._conn_ctx.reset(token)


@pytest.mark.parametrize("operation", ["delete", "latest"])
async def test_system_task_filters_bind_original_kind_string(operation: str) -> None:
    # Given
    db = AsyncMock(spec=DbConnection)
    db.fetchone.return_value = None
    token = session._conn_ctx.set(db)
    try:
        # When
        if operation == "delete":
            await notification_dao.delete_system_task_failed(user_id=UUID(int=1), agent_id=UUID(int=2))
            call = db.execute.call_args
        else:
            _ = await notification_dao.latest_system_task_failed(user_id=UUID(int=1), ref_id=UUID(int=2))
            call = db.fetchone.call_args
        # Then
        assert call is not None
        query, params = call.args
        assert "type = %(notification_kind)s" in query
        assert params["notification_kind"] == "system"
        assert type(params["notification_kind"]) is str
    finally:
        session._conn_ctx.reset(token)
