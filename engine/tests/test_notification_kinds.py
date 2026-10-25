from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from pydantic import TypeAdapter, ValidationError

from app.api import notification as notification_api
from app.core.notification_kinds import NotificationKind
from app.core.security import get_current_user
from app.records.notification import NotificationRecord
from app.records.user import UserRecord
from app.schemas.notification_responses import NotificationResponse
from notification_db_fakes import NotificationConnection, notification_db

__all__ = ["notification_db"]

KINDS: list[tuple[NotificationKind, str]] = [
    (NotificationKind.MENTION, "mention"),
    (NotificationKind.PLAZA_REPLY, "plaza_reply"),
    (NotificationKind.PLAZA_COMMENT, "plaza_comment"),
    (NotificationKind.BROADCAST, "broadcast"),
    (NotificationKind.APPROVAL_PENDING, "approval_pending"),
    (NotificationKind.APPROVAL_RESOLVED, "approval_resolved"),
    (NotificationKind.AUTONOMY_L2, "autonomy_l2"),
    (NotificationKind.COMPANY_ADMISSION_PENDING, "company_admission_pending"),
    (NotificationKind.COMPANY_ADMISSION_RESOLVED, "company_admission_resolved"),
    (NotificationKind.COMPANY_ADMISSION_CREATED, "company_admission_created"),
    (NotificationKind.AGENT_DELETION_REQUESTED, "agent_deletion_requested"),
    (NotificationKind.AGENT_DELETION_RESOLVED, "agent_deletion_resolved"),
    (NotificationKind.AGENT_OWNERSHIP_TRANSFERRED, "agent_ownership_transferred"),
    (NotificationKind.ORGANIZATION_WELCOME, "organization_welcome"),
    (NotificationKind.SYSTEM_ALERT, "system_alert"),
    (NotificationKind.SYSTEM, "system"),
    (NotificationKind.OPENCLAW_UPDATE, "openclaw_update"),
]


@pytest.mark.parametrize(("kind", "wire"), KINDS)
def test_database_kind_roundtrips_through_record_json(kind: NotificationKind, wire: str) -> None:
    # Given
    row = {"id": UUID(int=1), "type": wire, "title": "Update", "link": None, "sender_name": None}
    adapter = TypeAdapter(NotificationRecord)
    # When
    record = NotificationRecord.from_row(row)
    encoded = adapter.dump_json(record)
    decoded = adapter.validate_json(encoded)
    # Then
    assert record.type is kind
    assert decoded.type is kind
    assert TypeAdapter(dict[str, object]).validate_json(encoded)["type"] == wire


@pytest.mark.parametrize("wire", ["", "future_type", "system_task_failed", "BROADCAST"])
def test_database_rejects_unknown_notification_kind(wire: str) -> None:
    # Given
    row = {"id": UUID(int=1), "type": wire, "title": "Update", "link": None, "sender_name": None}
    # When / Then
    with pytest.raises(ValueError, match="NotificationKind"):
        _ = NotificationRecord.from_row(row)


def test_response_contract_rejects_unknown_notification_kind() -> None:
    # Given
    payload = b'{"id":"1","type":"future_type","title":"Update","body":"","link":null,"ref_id":null,"sender_name":null,"is_read":false,"archived_at":null,"created_at":null}'
    # When / Then
    with pytest.raises(ValidationError, match="enum"):
        _ = TypeAdapter(NotificationResponse).validate_json(payload)


async def test_http_returns_original_kind_strings_for_every_kind(notification_db: NotificationConnection) -> None:
    # Given
    member = UserRecord(id=UUID(int=100), tenant_id=UUID(int=200))
    notification_db.notifications = [
        NotificationRecord(id=UUID(int=index), user_id=member.id, type=kind, title="Update")
        for index, (kind, _) in enumerate(KINDS, start=1)
    ]
    app = FastAPI()
    app.include_router(notification_api.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: member
    try:
        # When
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/notifications", params={"category": "all"})
        # Then
        assert response.status_code == 200
        rows = TypeAdapter(list[dict[str, object]]).validate_json(response.content)
        assert {row["id"]: row["type"] for row in rows} == {
            str(UUID(int=index)): wire for index, (_, wire) in enumerate(KINDS, start=1)
        }
        typed = TypeAdapter(list[NotificationResponse]).validate_json(response.content)
        assert {row["type"] for row in typed} == set(NotificationKind)
    finally:
        app.dependency_overrides.clear()
