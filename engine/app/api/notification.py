"""Notification API - list, archive, read state, and broadcast."""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.notification_kinds import NotificationKind
from app.core.security import get_current_user
from app.dao.agent_dao import agent_dao
from app.dao.broadcast_email_dao import broadcast_email_dao
from app.dao.notification_dao import notification_dao
from app.dao.user_dao import user_dao
from app.db.session import connection_ctx, flush_request_transaction
from app.records.broadcast_email import BroadcastEmailStatus, NewBroadcastEmail
from app.records.user import UserRecord
from app.schemas.notification_responses import (
    NotificationBroadcastEmailCancellationResponse,
    NotificationBroadcastEmailStatusResponse,
    NotificationBroadcastResponse,
    NotificationReadResponse,
    NotificationResponse,
    NotificationUnreadCountResponse,
)

router = APIRouter(tags=["notifications"])

# Category -> type mapping for filtering
CATEGORY_TYPE_MAP: dict[str, list[NotificationKind]] = {
    "tool": [NotificationKind.AUTONOMY_L2],
    "approval": [
        NotificationKind.APPROVAL_PENDING,
        NotificationKind.APPROVAL_RESOLVED,
        NotificationKind.COMPANY_ADMISSION_PENDING,
        NotificationKind.COMPANY_ADMISSION_RESOLVED,
    ],
    "social": [
        NotificationKind.PLAZA_COMMENT,
        NotificationKind.PLAZA_REPLY,
        NotificationKind.MENTION,
        NotificationKind.BROADCAST,
    ],
    "broadcast": [NotificationKind.BROADCAST],
}


def _types_for_category(category: str | None) -> list[NotificationKind] | None:
    if category is None or category == "all":
        return None
    if category not in CATEGORY_TYPE_MAP:
        raise HTTPException(400, "Unknown notification category")
    return CATEGORY_TYPE_MAP[category]


@router.get("/notifications")
async def list_notifications(
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    unread_only: bool = Query(False),
    category: str | None = Query(None),
    view: Literal["active", "archived"] = Query("active"),
    current_user: UserRecord = Depends(get_current_user),
) -> list[NotificationResponse]:
    """List notifications for the current user, newest first."""
    notifications = await notification_dao.list_for_user(
        current_user.id,
        view=view,
        unread_only=unread_only,
        types=_types_for_category(category),
        limit=limit,
        offset=offset,
    )
    return [
        {
            "id": str(n.id),
            "type": n.type,
            "title": n.title,
            "body": n.body,
            "link": n.link,
            "ref_id": str(n.ref_id) if n.ref_id else None,
            "sender_name": n.sender_name,
            "is_read": n.is_read,
            "archived_at": n.archived_at.isoformat() if n.archived_at else None,
            "created_at": n.created_at.isoformat() if n.created_at else None,
        }
        for n in notifications
    ]


@router.get("/notifications/unread-count")
async def get_unread_count(
    category: str | None = Query(None), current_user: UserRecord = Depends(get_current_user)
) -> NotificationUnreadCountResponse:
    """Get the number of unread notifications for the current user."""
    count = await notification_dao.count_unread_for_user(
        current_user.id,
        types=_types_for_category(category),
    )
    return {"unread_count": count}


@router.post("/notifications/{notification_id}/read")
async def mark_read(
    notification_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)
) -> NotificationReadResponse:
    """Mark a single notification as read."""
    await notification_dao.mark_read_for_user(notification_id, current_user.id)
    return {"ok": True}


@router.post("/notifications/{notification_id}/archive")
async def archive_notification(
    notification_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)
) -> NotificationReadResponse:
    """Archive a notification belonging to the authenticated membership."""
    if not await notification_dao.archive_for_user(notification_id, current_user.id):
        raise HTTPException(404, "Notification not found")
    return {"ok": True}


@router.post("/notifications/{notification_id}/restore")
async def restore_notification(
    notification_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)
) -> NotificationReadResponse:
    """Restore a notification without changing whether it has been read."""
    if not await notification_dao.restore_for_user(notification_id, current_user.id):
        raise HTTPException(404, "Notification not found")
    return {"ok": True}


@router.post("/notifications/read-all")
async def mark_all_read(
    current_user: UserRecord = Depends(get_current_user),
    category: Annotated[str | None, Query()] = None,
) -> NotificationReadResponse:
    """Mark matching notifications as read for the current user."""
    await notification_dao.mark_all_read_for_user(current_user.id, types=_types_for_category(category))
    return {"ok": True}


# ── Broadcast ──────────────────────────────────────────


class BroadcastRequest(BaseModel):
    title: str = Field(..., max_length=200)
    body: str = Field("", max_length=1000)
    send_email: bool = False


def _broadcast_tenant(current_user: UserRecord) -> uuid.UUID:
    if current_user.role not in ("platform_admin", "org_admin"):
        raise HTTPException(403, "Only org admins can send broadcasts")
    if not current_user.tenant_id:
        raise HTTPException(400, "No tenant associated with your account")
    return current_user.tenant_id


def _email_status_response(status: BroadcastEmailStatus) -> NotificationBroadcastEmailStatusResponse:
    return {
        "broadcast_id": status.broadcast_id,
        "available": status.available,
        "queued": status.queued,
        "claimed": status.claimed,
        "smtp_accepted": status.smtp_accepted,
        "failed": status.failed,
        "cancelled": status.cancelled,
    }


@router.get("/notifications/broadcast/{broadcast_id}/email-status")
async def broadcast_email_status(
    broadcast_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)
) -> NotificationBroadcastEmailStatusResponse:
    status = await broadcast_email_dao.email_status(_broadcast_tenant(current_user), broadcast_id)
    return _email_status_response(status)


@router.post("/notifications/broadcast/{broadcast_id}/cancel-email")
async def cancel_broadcast_email(
    broadcast_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)
) -> NotificationBroadcastEmailCancellationResponse:
    result = await broadcast_email_dao.cancel_email(_broadcast_tenant(current_user), broadcast_id)
    return {**_email_status_response(result.status), "cancelled_now": result.cancelled_now}


@router.post("/notifications/broadcast")
async def broadcast_notification(
    req: BroadcastRequest,
    current_user: UserRecord = Depends(get_current_user),
) -> NotificationBroadcastResponse:
    """Send a notification to all users and agents in the current tenant.
    Requires org_admin or platform_admin role."""
    tenant_id = _broadcast_tenant(current_user)

    from app.services.notification_service import send_notification

    sender_name = current_user.display_name or current_user.email or "Admin"
    count_users = 0
    count_agents = 0
    count_emails = 0
    broadcast_id = uuid.uuid4()

    if req.send_email:
        from app.services.system_email_service import resolve_email_config_async

        email_config = await resolve_email_config_async(tenant_id)
        if not email_config:
            raise HTTPException(
                400,
                "Organization SMTP is not configured. Ask your genesis administrator to configure SMTP in Settings.",
            )

    async with connection_ctx():
        users = await user_dao.list_active_for_tenant(
            tenant_id,
            exclude_user_id=current_user.id,
            include_identity=req.send_email,
        )
        for user in users:
            _ = await send_notification(
                user_id=user.id,
                notification_kind=NotificationKind.BROADCAST,
                title=req.title,
                body=req.body,
                sender_name=sender_name,
            )
            count_users += 1

        agents = await agent_dao.list_for_tenant(tenant_id)
        for agent in agents:
            _ = await send_notification(
                agent_id=agent.id,
                notification_kind=NotificationKind.BROADCAST,
                title=req.title,
                body=req.body,
                sender_name=sender_name,
            )
            count_agents += 1

        if req.send_email:
            count_emails = await broadcast_email_dao.enqueue_many(
                [
                    NewBroadcastEmail(
                        broadcast_id=broadcast_id,
                        tenant_id=tenant_id,
                        user_id=user.id,
                        to_email=user.email,
                        subject=req.title,
                        body=(
                            f"{req.body}\n\nSent by: {sender_name}" if req.body.strip() else f"Sent by: {sender_name}"
                        ),
                    )
                    for user in users
                    if user.email not in (None, "")
                ]
            )
        await flush_request_transaction()

    return {
        "ok": True,
        "broadcast_id": broadcast_id,
        "users_notified": count_users,
        "agents_notified": count_agents,
        "emails_sent": count_emails,
    }
