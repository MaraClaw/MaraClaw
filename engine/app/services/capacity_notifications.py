"""In-app notifications of the capacity order workflow. Links are web-a paths."""

from collections.abc import Sequence
from uuid import UUID

from app.core.logging import logger
from app.core.notification_kinds import NotificationKind
from app.dao.user_dao import user_dao
from app.records.capacity_order import CapacityOrderRecord, CapacityOrderStatus
from app.records.user import UserRecord
from app.services.notification_service import send_notification


def order_link(order_id: UUID) -> str:
    """Where an organization admin follows an order."""
    return f"/capacity-orders/{order_id}"


def review_link(order_id: UUID) -> str:
    """Where a platform admin reviews an order."""
    return f"/capacity-payments/{order_id}"


def _active(user: UserRecord) -> bool:
    return user.is_active and (user.identity is None or user.identity.is_active)


def format_usd(cents: int) -> str:
    return f"${cents // 100:,}.{cents % 100:02d}"


def format_vnd(amount: int) -> str:
    return f"{amount:,} VND"


def _describe(order: CapacityOrderRecord) -> str:
    parts = [
        f"{count} {noun}{'s' if count != 1 else ''}"
        for count, noun in ((order.extra_accounts, "extra account"), (order.extra_agents, "extra agent"))
        if count
    ]
    return " and ".join(parts)


async def active_org_admins(tenant_id: UUID) -> Sequence[UserRecord]:
    return [admin for admin in await user_dao.list_org_admins_for_tenant(tenant_id) if _active(admin)]


async def notify_platform_admins_submitted(order: CapacityOrderRecord, tenant_name: str) -> int:
    """Tell every active platform admin that evidence is waiting for review; the number notified."""
    admins = [admin for admin in await user_dao.list_active_platform_admins() if _active(admin)]
    if not admins:
        logger.warning("[capacity] no active platform admin to notify about order {}", order.reference)
    for admin in admins:
        _ = await send_notification(
            admin.id,
            notification_kind=NotificationKind.CAPACITY_PAYMENT_SUBMITTED,
            title="Capacity payment to review",
            body=(
                f"{tenant_name} paid {format_vnd(order.amount_vnd)} for {_describe(order)} "
                f"(reference {order.reference})."
            ),
            link=review_link(order.id),
            ref_id=order.id,
        )
    return len(admins)


async def notify_decision(order: CapacityOrderRecord) -> int:
    """Tell the submitter and every active org admin of the company how the review ended; the number notified."""
    recipients = {admin.id for admin in await active_org_admins(order.tenant_id)}
    if order.submitted_by is not None:
        submitter = await user_dao.get(order.submitted_by)
        if submitter is not None and submitter.tenant_id == order.tenant_id and submitter.is_active:
            recipients.add(submitter.id)
    approved = order.status is CapacityOrderStatus.APPROVED
    note = f" Reason: {order.review_note}" if not approved and order.review_note is not None else ""
    for user_id in sorted(recipients, key=str):
        _ = await send_notification(
            user_id,
            notification_kind=NotificationKind.CAPACITY_PAYMENT_APPROVED
            if approved
            else NotificationKind.CAPACITY_PAYMENT_REJECTED,
            title="Extra accounts and agents approved" if approved else "Extra accounts and agents payment rejected",
            body=(
                f"Payment {order.reference} for {_describe(order)} was {'approved' if approved else 'rejected'}.{note}"
            ),
            link=order_link(order.id),
            ref_id=order.id,
        )
    return len(recipients)


async def notify_renewal_due(order: CapacityOrderRecord) -> int:
    """Ask every active org admin of the company to pay a renewal; the number notified."""
    admins = await active_org_admins(order.tenant_id)
    grace = f" Extras keep working until {order.grace_until.isoformat()}." if order.grace_until is not None else ""
    for admin in admins:
        _ = await send_notification(
            admin.id,
            notification_kind=NotificationKind.CAPACITY_RENEWAL_DUE,
            title="Extra accounts and agents renewal due",
            body=(f"Renewal {order.reference} for {_describe(order)} is {format_vnd(order.amount_vnd)}.{grace}"),
            link=order_link(order.id),
            ref_id=order.id,
        )
    return len(admins)
