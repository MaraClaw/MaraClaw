"""Notification API — list, count, mark-read, and broadcast."""

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.security import get_current_user
from app.dao.agent_dao import agent_dao
from app.dao.notification_dao import notification_dao
from app.dao.user_dao import user_dao
from app.records.user import UserRecord
from app.services.system_email_service import BroadcastEmailRecipient, deliver_broadcast_emails

router = APIRouter(tags=["notifications"])

# Category -> type mapping for filtering
CATEGORY_TYPE_MAP: dict[str, list[str]] = {
    "tool": ["autonomy_l2"],
    "approval": ["approval_pending", "approval_resolved"],
    "social": ["plaza_comment", "plaza_reply", "mention", "broadcast"],
    "broadcast": ["broadcast"],
}


def _types_for_category(category: str | None) -> list[str] | None:
    if category and category != "all" and category in CATEGORY_TYPE_MAP:
        return CATEGORY_TYPE_MAP[category]
    return None


@router.get("/notifications")
async def list_notifications(
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    unread_only: bool = Query(False),
    category: str | None = Query(None),
    current_user: UserRecord = Depends(get_current_user),
):
    """List notifications for the current user, newest first."""
    notifications = await notification_dao.list_for_user(
        current_user.id,
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
            "created_at": n.created_at.isoformat() if n.created_at else None,
        }
        for n in notifications
    ]


@router.get("/notifications/unread-count")
async def get_unread_count(category: str | None = Query(None), current_user: UserRecord = Depends(get_current_user)):
    """Get the number of unread notifications for the current user."""
    count = await notification_dao.count_unread_for_user(
        current_user.id,
        types=_types_for_category(category),
    )
    return {"unread_count": count}


@router.post("/notifications/{notification_id}/read")
async def mark_read(notification_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)):
    """Mark a single notification as read."""
    await notification_dao.mark_read_for_user(notification_id, current_user.id)
    return {"ok": True}


@router.post("/notifications/read-all")
async def mark_all_read(current_user: UserRecord = Depends(get_current_user)):
    """Mark all notifications as read for the current user."""
    await notification_dao.mark_all_read_for_user(current_user.id)
    return {"ok": True}


# ── Broadcast ──────────────────────────────────────────


class BroadcastRequest(BaseModel):
    title: str = Field(..., max_length=200)
    body: str = Field("", max_length=1000)
    send_email: bool = False


@router.post("/notifications/broadcast")
async def broadcast_notification(
    req: BroadcastRequest,
    background_tasks: BackgroundTasks,
    current_user: UserRecord = Depends(get_current_user),
    db=None,
):
    """Send a notification to all users and agents in the current tenant.
    Requires org_admin or platform_admin role."""
    if current_user.role not in ("platform_admin", "org_admin"):
        raise HTTPException(403, "Only org admins can send broadcasts")
    if not current_user.tenant_id:
        raise HTTPException(400, "No tenant associated with your account")

    from app.services.notification_service import send_notification

    tenant_id = current_user.tenant_id
    sender_name = current_user.display_name or current_user.username or "Admin"
    count_users = 0
    count_agents = 0
    count_emails = 0
    email_recipients: list[BroadcastEmailRecipient] = []

    if req.send_email:
        from app.services.system_email_service import resolve_email_config_async

        email_config = await resolve_email_config_async(db)
        if not email_config:
            raise HTTPException(400, "System email is not configured. Please configure it in Platform Settings.")

    users = await user_dao.list_active_for_tenant(
        tenant_id,
        exclude_user_id=current_user.id,
        include_identity=req.send_email,
    )
    for user in users:
        await send_notification(
            None,
            user_id=user.id,
            type="broadcast",
            title=req.title,
            body=req.body,
            sender_name=sender_name,
        )
        count_users += 1

    agents = await agent_dao.list_for_tenant(tenant_id)
    for agent in agents:
        await send_notification(
            None,
            agent_id=agent.id,
            type="broadcast",
            title=req.title,
            body=req.body,
            sender_name=sender_name,
        )
        count_agents += 1

    if req.send_email:
        for user in users:
            if not user.email:
                continue
            email_recipients.append(
                BroadcastEmailRecipient(
                    email=user.email,
                    subject=req.title,
                    body=(f"{req.body}\n\nSent by: {sender_name}" if req.body.strip() else f"Sent by: {sender_name}"),
                ),
            )
            count_emails += 1

    if email_recipients:
        background_tasks.add_task(deliver_broadcast_emails, email_recipients)
    return {
        "ok": True,
        "users_notified": count_users,
        "agents_notified": count_agents,
        "emails_sent": count_emails,
    }
