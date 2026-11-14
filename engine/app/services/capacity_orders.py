"""Buying extra accounts and agents: quote, bank-transfer payment, evidence upload and platform-admin review.

The organization admin enters quantities, the engine prices them (``capacity_pricing``), the company transfers the
amount and attaches evidence, and any active platform admin approves or rejects it. Approved orders are what
``capacity_ledger`` turns into effective extras. Each transition is an UPDATE guarded by the status it expects, made
under the order row lock (and, for decisions, the tenant row lock that also serializes the renewal worker).
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import assert_never
from uuid import UUID

from fastapi import HTTPException

from app.core.acl_locks import lock_tenants
from app.core.domain.approvals import ApprovalAction
from app.core.domain.users import UserRole
from app.core.logging import logger
from app.core.security import load_user_principal
from app.dao.capacity_order_dao import capacity_order_dao as orders
from app.dao.tenant_dao import tenant_dao
from app.db.session import connection_ctx
from app.records.capacity_order import (
    CapacityEvidence,
    CapacityOrderKind,
    CapacityOrderRecord,
    CapacityOrderStatus,
    NewCapacityOrder,
)
from app.records.organization_plan import OrganizationPlanId
from app.records.user import UserRecord
from app.services import capacity_ledger
from app.services.admin_audit import field_change, write_admin_audit
from app.services.capacity_evidence import (
    MAX_EVIDENCE_BYTES,
    EvidenceKind,
    safe_filename,
    sha256_hex,
    sniff_evidence,
    storage_key,
)
from app.services.capacity_notifications import notify_decision, notify_platform_admins_submitted
from app.services.capacity_payment_settings import load_payment_settings
from app.services.capacity_pricing import days_in_month, month_start, quote_purchase, remaining_days
from app.services.capacity_renewals import eligible_tier, supplement_late_approval
from app.services.company_capacity import company_caps, company_usage
from app.services.organization_plans import plan_for
from app.services.storage_runtime.facade import get_storage_backend

EVIDENCE_WRITE_ATTEMPTS = 3


def conflict(code: str, message: str, **extra: str | int) -> HTTPException:
    return HTTPException(409, detail={"code": code, "message": message, **extra})


@dataclass(frozen=True, slots=True)
class CapacityOrderView:
    order: CapacityOrderRecord
    tenant_name: str | None


@dataclass(frozen=True, slots=True)
class CapacityOverview:
    tenant_id: UUID
    capped: bool
    tier: OrganizationPlanId | None
    included_accounts: int | None
    included_agents: int | None
    extra_accounts: int
    extra_agents: int
    account_limit: int | None
    agent_limit: int | None
    accounts_used: int
    agents_used: int
    account_unit_usd: int | None
    agent_unit_usd: int | None
    today: date
    days_in_month: int
    remaining_days: int
    payments_configured: bool
    usd_vnd_rate: int | None
    open_orders: list[CapacityOrderRecord]
    current_renewal: CapacityOrderRecord | None


@dataclass(frozen=True, slots=True)
class EvidenceFile:
    data: bytes
    content_type: str
    filename: str


async def _principal(user_id: UUID) -> UserRecord:
    return await load_user_principal(user_id, fresh=True)


def _require_org_admin(user: UserRecord) -> UUID:
    if user.role is not UserRole.ORG_ADMIN or user.is_platform_admin or user.tenant_id is None:
        raise HTTPException(403, "Only an organization admin may buy or manage extra accounts and agents")
    return user.tenant_id


def _require_platform_admin(user: UserRecord) -> None:
    if not user.is_platform_admin:
        raise HTTPException(403, "Platform admin access required")


def _require_reader(user: UserRecord) -> None:
    """Platform admins read any company; organization admins read their own."""
    if not user.is_platform_admin:
        _ = _require_org_admin(user)


def _reader_tenant(user: UserRecord, tenant_id: UUID | None) -> UUID:
    """The company a reader looks at: an org admin's own, or the one a platform admin names."""
    if user.is_platform_admin:
        if tenant_id is None:
            raise HTTPException(400, "tenant_id is required for platform admins")
        return tenant_id
    own = _require_org_admin(user)
    if tenant_id is not None and tenant_id != own:
        raise HTTPException(403, "Organization admins may only read their own company")
    return own


def _visible(user: UserRecord, order: CapacityOrderRecord | None) -> CapacityOrderRecord:
    if order is None or (not user.is_platform_admin and order.tenant_id != user.tenant_id):
        raise HTTPException(404, "Capacity order not found")
    return order


async def _view(order: CapacityOrderRecord) -> CapacityOrderView:
    return (await _views([order]))[0]


async def _views(records: list[CapacityOrderRecord]) -> list[CapacityOrderView]:
    tenants = {tenant.id: tenant.name for tenant in await tenant_dao.get_many(list({o.tenant_id for o in records}))}
    return [CapacityOrderView(order, tenants.get(order.tenant_id)) for order in records]


def _audit_details(order: CapacityOrderRecord) -> dict[str, object]:
    return {
        "order_id": str(order.id),
        "reference": order.reference,
        "kind": order.kind.value,
        "billing_month": order.billing_month.isoformat(),
        "extra_accounts": order.extra_accounts,
        "extra_agents": order.extra_agents,
        "amount_vnd": order.amount_vnd,
    }


async def overview(user_id: UUID, tenant_id: UUID | None) -> CapacityOverview:
    async with connection_ctx() as db:
        user = await _principal(user_id)
        target = _reader_tenant(user, tenant_id)
        if await tenant_dao.get(target) is None:
            raise HTTPException(404, "Company not found")
        today = capacity_ledger.vn_today()
        caps = await company_caps(db, target)
        accounts_used, agents_used = await company_usage(db, target)
        settings = await load_payment_settings()
        plan = plan_for(caps.tier) if caps is not None else None
        return CapacityOverview(
            tenant_id=target,
            capped=caps is not None,
            tier=caps.tier if caps is not None else None,
            included_accounts=plan.included_accounts if plan is not None else None,
            included_agents=int(plan.included_agents) if plan is not None else None,
            extra_accounts=caps.extras.accounts if caps is not None else 0,
            extra_agents=caps.extras.agents if caps is not None else 0,
            account_limit=caps.account_limit if caps is not None else None,
            agent_limit=caps.agent_limit if caps is not None else None,
            accounts_used=accounts_used,
            agents_used=agents_used,
            account_unit_usd=int(plan.additional_account_monthly_usd) if plan is not None else None,
            agent_unit_usd=int(plan.additional_agent_monthly_usd) if plan is not None else None,
            today=today,
            days_in_month=days_in_month(today),
            remaining_days=remaining_days(today),
            payments_configured=settings is not None,
            usd_vnd_rate=settings.usd_vnd_rate if settings is not None else None,
            open_orders=await orders.open_for_tenant(target),
            current_renewal=await orders.primary_renewal(target, month_start(today)),
        )


async def create_order(
    user_id: UUID, extra_accounts: int, extra_agents: int, *, client_ip: str | None = None
) -> CapacityOrderView:
    """Price a purchase, lock the quote, the rate and the bank recipient on the order and open it for payment."""
    async with connection_ctx() as db:
        tenant_id = _require_org_admin(await _principal(user_id))
        await lock_tenants(db, (tenant_id,))
        actor = await _principal(user_id)
        _ = _require_org_admin(actor)
        tier = await eligible_tier(db, tenant_id)
        if tier is None:
            raise conflict("not_eligible", "Only companies on a paid plan can buy extra accounts and agents")
        settings = await load_payment_settings()
        if settings is None:
            raise conflict("payments_not_configured", "Payments are not configured yet. Contact the platform admin.")
        open_purchase = await orders.open_purchase(tenant_id)
        if open_purchase is not None:
            raise conflict(
                "purchase_already_open",
                "Finish or cancel the open purchase before starting another",
                order_id=str(open_purchase.id),
            )
        plan = plan_for(tier)
        quote = quote_purchase(
            capacity_ledger.vn_today(),
            extra_accounts=extra_accounts,
            extra_agents=extra_agents,
            account_unit_usd=int(plan.additional_account_monthly_usd),
            agent_unit_usd=int(plan.additional_agent_monthly_usd),
            usd_vnd_rate=settings.usd_vnd_rate,
        )
        order = await orders.insert(
            NewCapacityOrder(
                tenant_id=tenant_id,
                kind=CapacityOrderKind.PURCHASE,
                tier=tier,
                billing_month=quote.billing_month,
                extra_accounts=quote.extra_accounts,
                extra_agents=quote.extra_agents,
                account_unit_usd=quote.account_unit_usd,
                agent_unit_usd=quote.agent_unit_usd,
                days_charged=quote.days_charged,
                days_in_month=quote.days_in_month,
                amount_usd_cents=quote.amount_usd_cents,
                usd_vnd_rate=quote.usd_vnd_rate,
                amount_vnd=quote.amount_vnd,
                recipient=settings.recipient,
                created_by=actor.id,
            )
        )
        await write_admin_audit(
            actor=actor,
            action="capacity_order_create",
            target_type="capacity_order",
            target_id=order.id,
            tenant_id=tenant_id,
            changes={"status": field_change(None, order.status.value)},
            details=_audit_details(order),
            ip_address=client_ip,
        )
        return await _view(order)


async def list_company_orders(
    user_id: UUID, tenant_id: UUID | None, *, limit: int, offset: int
) -> tuple[list[CapacityOrderView], bool]:
    async with connection_ctx():
        user = await _principal(user_id)
        target = _reader_tenant(user, tenant_id)
        rows = await orders.list_for_tenant(target, limit=limit + 1, offset=offset)
        return await _views(rows[:limit]), len(rows) > limit


async def list_review_orders(
    user_id: UUID, status: CapacityOrderStatus | None, *, limit: int, offset: int
) -> tuple[list[CapacityOrderView], bool]:
    async with connection_ctx():
        _require_platform_admin(await _principal(user_id))
        rows = await orders.list_by_status(status, limit=limit + 1, offset=offset)
        return await _views(rows[:limit]), len(rows) > limit


async def get_order(user_id: UUID, order_id: UUID, *, platform_only: bool = False) -> CapacityOrderView:
    async with connection_ctx():
        user = await _principal(user_id)
        if platform_only:
            _require_platform_admin(user)
        else:
            _require_reader(user)
        return await _view(_visible(user, await orders.get(order_id)))


async def _store_evidence(tenant_id: UUID, order_id: UUID, kind: EvidenceKind, data: bytes) -> str:
    storage = get_storage_backend()
    for _ in range(EVIDENCE_WRITE_ATTEMPTS):
        key = storage_key(tenant_id, order_id, kind)
        written = await storage.create_bytes_if_absent(key, data, kind.content_type)
        if written.ok:
            return key
    raise HTTPException(503, "Could not store the payment evidence; try again")


async def _discard_evidence(key: str) -> None:
    try:
        await get_storage_backend().delete(key)
    except Exception as exc:
        logger.bind(exception_type=type(exc).__name__).warning("[capacity] could not remove unused evidence {}", key)


async def submit_evidence(
    user_id: UUID, order_id: UUID, *, data: bytes, filename: str | None, client_ip: str | None = None
) -> CapacityOrderView:
    """Attach the transfer evidence and send the order for review ("Finish"): only from ``awaiting_payment``."""
    if len(data) > MAX_EVIDENCE_BYTES:
        raise HTTPException(413, "Payment evidence must be 10 MiB or smaller")
    kind = sniff_evidence(data)
    if kind is None:
        raise HTTPException(415, "Payment evidence must be a PNG, JPEG or WebP image or a PDF")
    async with connection_ctx():
        user = await _principal(user_id)
        tenant_id = _require_org_admin(user)
        preview = _visible(user, await orders.get(order_id))
        if preview.status is not CapacityOrderStatus.AWAITING_PAYMENT:
            raise conflict("order_not_awaiting_payment", "This order no longer accepts payment evidence")
    key = await _store_evidence(tenant_id, order_id, kind, data)
    evidence = CapacityEvidence(
        storage_key=key,
        content_type=kind.content_type,
        filename=safe_filename(filename, kind),
        size_bytes=len(data),
        sha256=sha256_hex(data),
    )
    try:
        async with connection_ctx():
            actor = await _principal(user_id)
            _ = _require_org_admin(actor)
            locked = _visible(actor, await orders.get_locked(order_id))
            submitted = await orders.mark_submitted(locked.id, evidence, user_id=actor.id, now=datetime.now(UTC))
            if submitted is None:
                raise conflict("order_not_awaiting_payment", "This order no longer accepts payment evidence")
            tenant = await tenant_dao.get(tenant_id)
            _ = await notify_platform_admins_submitted(submitted, tenant.name if tenant is not None else "A company")
            await write_admin_audit(
                actor=actor,
                action="capacity_order_submit",
                target_type="capacity_order",
                target_id=submitted.id,
                tenant_id=tenant_id,
                changes={"status": field_change(locked.status.value, submitted.status.value)},
                details={**_audit_details(submitted), "evidence_sha256": evidence.sha256},
                ip_address=client_ip,
            )
            return await _view(submitted)
    except BaseException:
        await _discard_evidence(key)
        raise


async def cancel_order(user_id: UUID, order_id: UUID, *, client_ip: str | None = None) -> CapacityOrderView:
    """Withdraw an unpaid purchase. Renewals cannot be cancelled; cancelling twice answers the same."""
    async with connection_ctx():
        user = await _principal(user_id)
        tenant_id = _require_org_admin(user)
        _ = _visible(user, await orders.get(order_id))
        locked = _visible(user, await orders.get_locked(order_id))
        actor = await _principal(user_id)
        _ = _require_org_admin(actor)
        if locked.status is CapacityOrderStatus.CANCELLED:
            return await _view(locked)
        if locked.kind is not CapacityOrderKind.PURCHASE or locked.status is not CapacityOrderStatus.AWAITING_PAYMENT:
            raise conflict("order_not_cancellable", "Only a purchase that is still awaiting payment can be cancelled")
        cancelled = await orders.mark_cancelled(locked.id)
        if cancelled is None:
            raise conflict("order_not_cancellable", "Only a purchase that is still awaiting payment can be cancelled")
        await write_admin_audit(
            actor=actor,
            action="capacity_order_cancel",
            target_type="capacity_order",
            target_id=cancelled.id,
            tenant_id=tenant_id,
            changes={"status": field_change(locked.status.value, cancelled.status.value)},
            details=_audit_details(cancelled),
            ip_address=client_ip,
        )
        return await _view(cancelled)


async def read_evidence(user_id: UUID, order_id: UUID) -> EvidenceFile:
    """The stored evidence bytes, for platform admins and the org admins of the order's company."""
    async with connection_ctx():
        user = await _principal(user_id)
        _require_reader(user)
        order = _visible(user, await orders.get(order_id))
    if order.evidence is None:
        raise HTTPException(404, "This order has no payment evidence")
    try:
        data = await get_storage_backend().read_bytes_bounded(order.evidence.storage_key, MAX_EVIDENCE_BYTES)
    except FileNotFoundError:
        raise HTTPException(404, "The payment evidence file is missing") from None
    return EvidenceFile(data=data, content_type=order.evidence.content_type, filename=order.evidence.filename)


async def decide(
    reviewer_id: UUID, order_id: UUID, action: ApprovalAction, note: str | None, *, client_ip: str | None = None
) -> CapacityOrderView:
    """Approve or reject a submitted order. The same decision repeated replays it; a different one is a 409."""
    match action:
        case ApprovalAction.APPROVE:
            target = CapacityOrderStatus.APPROVED
        case ApprovalAction.REJECT:
            target = CapacityOrderStatus.REJECTED
        case unreachable:
            assert_never(unreachable)
    async with connection_ctx() as db:
        _require_platform_admin(await _principal(reviewer_id))
        snapshot = await orders.get(order_id)
        if snapshot is None:
            raise HTTPException(404, "Capacity order not found")
        await lock_tenants(db, (snapshot.tenant_id,))
        order = await orders.get_locked(order_id)
        if order is None:
            raise HTTPException(404, "Capacity order not found")
        reviewer = await _principal(reviewer_id)
        _require_platform_admin(reviewer)
        if order.status in (CapacityOrderStatus.APPROVED, CapacityOrderStatus.REJECTED):
            if order.status is target:
                return await _view(order)
            raise conflict("order_already_decided", "This order already has a different decision")
        if order.status is not CapacityOrderStatus.SUBMITTED:
            raise conflict("order_not_submitted", "Only an order with payment evidence can be reviewed")
        decided = await orders.mark_decided(order.id, target, reviewer_id=reviewer.id, note=note, now=datetime.now(UTC))
        if decided is None:
            raise conflict("order_not_submitted", "Only an order with payment evidence can be reviewed")
        supplement: CapacityOrderRecord | None = None
        if target is CapacityOrderStatus.APPROVED:
            supplement = await supplement_late_approval(
                db, decided, capacity_ledger.vn_today(), await load_payment_settings()
            )
        _ = await notify_decision(decided)
        details = _audit_details(decided)
        if supplement is not None:
            details["supplementary_renewal_id"] = str(supplement.id)
        await write_admin_audit(
            actor=reviewer,
            action="capacity_order_approve" if target is CapacityOrderStatus.APPROVED else "capacity_order_reject",
            target_type="capacity_order",
            target_id=decided.id,
            tenant_id=decided.tenant_id,
            changes={"status": field_change(order.status.value, decided.status.value)},
            details={**details, "note": note},
            ip_address=client_ip,
        )
        return await _view(decided)
