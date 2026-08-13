"""Notification service — unified entry point for sending in-app notifications."""

from __future__ import annotations

import uuid
from typing import Any

from app.core.logging import logger
from app.dao.notification_dao import notification_dao
from app.records.notification import NotificationRecord


async def send_notification(
    db: Any = None,
    user_id: uuid.UUID | None = None,
    *,
    agent_id: uuid.UUID | None = None,
    type: str,
    title: str,
    body: str = "",
    link: str | None = None,
    ref_id: uuid.UUID | None = None,
    sender_name: str | None = None,
) -> NotificationRecord:
    """Create and persist a notification for a user or an agent.

    ``db`` is accepted for call-site compatibility and ignored (psycopg path).
    """
    del db
    if not user_id and not agent_id:
        raise ValueError("Either user_id or agent_id must be provided")

    notif = await notification_dao.create(
        obj_in={
            "user_id": user_id,
            "agent_id": agent_id,
            "type": type,
            "title": title,
            "body": body,
            "link": link,
            "ref_id": ref_id,
            "sender_name": sender_name,
            "is_read": False,
        }
    )
    recipient = f"user {user_id}" if user_id else f"agent {agent_id}"
    logger.info(f"Notification [{type}] sent to {recipient}: {title}")
    return notif
