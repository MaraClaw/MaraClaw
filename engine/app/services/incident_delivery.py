"""Best-effort incident fan-out, called by the consumer in a fresh Context."""

from email.utils import make_msgid
from typing import assert_never

from anyio import CapacityLimiter, create_task_group

from app.core.logging import logger
from app.core.notification_kinds import NotificationKind
from app.dao import user_dao
from app.records.user import UserRecord
from app.services.incident_report import IncidentReport
from app.services.notification_service import send_notification
from app.services.system_email_service import attempt_email


async def deliver_incident(report: IncidentReport) -> bool:
    """Attempt every membership and unique email; succeed if any channel succeeds.

    Ordinary exceptions are contained at discovery/channel boundaries so failures
    cannot cancel peer deliveries. Cancellation is deliberately not intercepted.
    DAO calls own their short transactions; no transaction encloses SMTP.
    """
    try:
        recipients = await user_dao.list_active_platform_admins()
    except Exception as exc:
        logger.bind(exception_type=type(exc).__name__).warning("Incident recipient discovery failed")
        return False
    if not recipients:
        return True

    limiter = CapacityLimiter(4)
    delivered = False

    async def deliver_recipient(user: UserRecord, email: str | None) -> None:
        nonlocal delivered
        async with limiter:
            try:
                await send_notification(
                    user.id,
                    notification_kind=NotificationKind.SYSTEM_ALERT,
                    title=report.title,
                    body=report.body,
                    sender_name="System",
                )
                delivered = True
            except Exception as exc:
                logger.bind(exception_type=type(exc).__name__).warning("Incident notification delivery failed")

            if email is None:
                return
            try:
                outcome = await attempt_email(to=email, subject=report.title, body=report.body, message_id=make_msgid())
                match outcome.kind:
                    case "sent":
                        delivered = True
                    case "not_configured" | "permanent" | "transient":
                        logger.bind(outcome_kind=outcome.kind).warning("Incident email delivery unsuccessful")
                    case unreachable:
                        assert_never(unreachable)
            except Exception as exc:
                logger.bind(exception_type=type(exc).__name__).warning("Incident email delivery failed")

    emails: set[str] = set()
    async with create_task_group() as tasks:
        for user in recipients:
            email = user.email
            if email is not None and email != "" and email.casefold() not in emails:
                emails.add(email.casefold())
                tasks.start_soon(deliver_recipient, user, email)
            else:
                tasks.start_soon(deliver_recipient, user, None)
    return delivered
