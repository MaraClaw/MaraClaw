from base64 import b64decode, urlsafe_b64encode
from binascii import Error as Base64Error
from typing import ClassVar
from uuid import UUID

from fastapi import HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from typing_extensions import TypedDict

from app.core.notification_kinds import NotificationKind
from app.records.notification import NotificationRecord


class NotificationCursor(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(strict=True, ge=1, le=1)
    created_at: AwareDatetime
    id: UUID

    def encode(self) -> str:
        return urlsafe_b64encode(self.model_dump_json().encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, value: str) -> NotificationCursor:
        if not value or len(value) > 1024:
            raise HTTPException(400, "Invalid cursor")
        try:
            payload = b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
            position = cls.model_validate_json(payload, strict=True)
        except (Base64Error, ValueError) as exc:
            raise HTTPException(400, "Invalid cursor") from exc
        if urlsafe_b64encode(payload).decode().rstrip("=") != value:
            raise HTTPException(400, "Invalid cursor")
        return position


class NotificationResponse(TypedDict):
    id: str
    type: NotificationKind
    title: str
    body: str
    link: str | None
    ref_id: str | None
    sender_name: str | None
    is_read: bool
    archived_at: str | None
    created_at: str | None


def notification_response(notification: NotificationRecord) -> NotificationResponse:
    return {
        "id": str(notification.id),
        "type": notification.type,
        "title": notification.title,
        "body": notification.body,
        "link": notification.link,
        "ref_id": str(notification.ref_id) if notification.ref_id else None,
        "sender_name": notification.sender_name,
        "is_read": notification.is_read,
        "archived_at": notification.archived_at.isoformat() if notification.archived_at else None,
        "created_at": notification.created_at.isoformat() if notification.created_at else None,
    }


class NotificationUnreadCountResponse(TypedDict):
    unread_count: int


class NotificationPageResponse(TypedDict):
    items: list[NotificationResponse]
    next_cursor: str | None


class NotificationReadResponse(TypedDict):
    ok: bool


class NotificationBroadcastResponse(NotificationReadResponse):
    broadcast_id: UUID
    users_notified: int
    agents_notified: int
    emails_sent: int


class NotificationBroadcastEmailStatusResponse(TypedDict):
    broadcast_id: UUID
    available: bool
    queued: int
    claimed: int
    smtp_accepted: int
    failed: int
    cancelled: int


class NotificationBroadcastEmailCancellationResponse(NotificationBroadcastEmailStatusResponse):
    cancelled_now: int
