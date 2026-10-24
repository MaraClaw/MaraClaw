from uuid import UUID

from typing_extensions import TypedDict


class NotificationResponse(TypedDict):
    id: str
    type: str
    title: str
    body: str
    link: str | None
    ref_id: str | None
    sender_name: str | None
    is_read: bool
    archived_at: str | None
    created_at: str | None


class NotificationUnreadCountResponse(TypedDict):
    unread_count: int


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
