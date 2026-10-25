from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.core.notification_kinds import NotificationKind
from app.records.notification import NotificationRecord
from app.services import notification_service


@pytest.mark.parametrize("recipient", ["user", "agent"])
@pytest.mark.parametrize("notification_kind", list(NotificationKind))
async def test_notification_kind_is_persisted_as_type(
    monkeypatch: pytest.MonkeyPatch, recipient: str, notification_kind: NotificationKind
) -> None:
    # Given: either supported recipient and an existing notification kind.
    recipient_id = uuid4()
    user_id = recipient_id if recipient == "user" else None
    agent_id = recipient_id if recipient == "agent" else None
    ref_id = uuid4()
    stored = NotificationRecord(id=uuid4(), type=notification_kind, title="Update")
    create = AsyncMock(return_value=stored)
    monkeypatch.setattr(notification_service.notification_dao, "create", create)

    # When: the typed service keyword is used.
    result = await notification_service.send_notification(
        user_id,
        agent_id=agent_id,
        notification_kind=notification_kind,
        title="Update",
        body="Details",
        link="/app/notifications",
        ref_id=ref_id,
        sender_name="System",
    )

    # Then: storage keeps its original type field and the other notification fields.
    create.assert_awaited_once_with(
        obj_in={
            "user_id": user_id,
            "agent_id": agent_id,
            "type": notification_kind.value,
            "title": "Update",
            "body": "Details",
            "link": "/app/notifications",
            "ref_id": ref_id,
            "sender_name": "System",
            "is_read": False,
        }
    )
    assert create.call_args is not None
    assert type(create.call_args.kwargs["obj_in"]["type"]) is str
    assert result is stored
