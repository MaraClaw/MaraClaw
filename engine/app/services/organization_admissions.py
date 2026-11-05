"""Public organization admission requests and their genesis platform-admin review.

Approving a named request provisions the organization in the same transaction: tenant, genesis org admin
and a durable, encrypted initial-credential email intent. The email is sent after commit by
``organization_admission_email_worker`` over the platform SMTP.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import assert_never
from uuid import UUID

from fastapi import HTTPException

from app.core.domain.approvals import ApprovalAction, ApprovalStatus
from app.core.notification_kinds import NotificationKind
from app.core.security import load_user_principal
from app.dao.organization_admission_dao import organization_admission_dao as admissions
from app.dao.organization_admission_email_dao import organization_admission_email_dao as emails
from app.dao.user_dao import user_dao
from app.db.session import connection_ctx
from app.records.organization_admission import OrganizationAdmissionRecord
from app.records.organization_admission_email import CredentialEmailState, NewCredentialEmail
from app.records.organization_plan import OrganizationPlanId
from app.records.user import UserRecord
from app.schemas.organization_admission import OrganizationAdmissionDecisionRequest
from app.services.admin_audit import field_change, write_admin_audit
from app.services.admin_provisioning import is_genesis_platform_admin
from app.services.notification_service import send_notification
from app.services.org_membership import DomainClaimedError
from app.services.organization_admission_credentials import fingerprint, new_password, seal
from app.services.organization_plans import plan_for
from app.services.tenant_provisioning import AdminEmailTakenError, create_tenant_with_org_admin

REVIEW_LINK = "/organization-admissions"


async def require_genesis_platform_admin(user_id: UUID) -> UserRecord:
    """Active genesis platform admin from fresh persisted membership and identity (never a token claim)."""
    user = await load_user_principal(user_id, fresh=True)
    if user.identity is None or not await is_genesis_platform_admin(user):
        raise HTTPException(403, "Only the genesis platform administrator may review organization admissions")
    return user


@dataclass(frozen=True, slots=True)
class OrganizationAdmissionView:
    request: OrganizationAdmissionRecord
    credential_email: CredentialEmailState | None


async def _views(records: list[OrganizationAdmissionRecord]) -> list[OrganizationAdmissionView]:
    states = await emails.states([record.id for record in records])
    return [OrganizationAdmissionView(record, states.get(record.id)) for record in records]


async def submit_request(email: str, organization_name: str, tier: OrganizationPlanId) -> None:
    """Persist one pending request per normalized email and notify the genesis platform admin exactly once."""
    plan = plan_for(tier)
    async with connection_ctx():
        recipient = await user_dao.genesis_platform_admin()
        if recipient is None or not recipient.is_active:
            raise HTTPException(503, "Organization admissions are temporarily unavailable")
        created = await admissions.insert_pending(email=email, organization_name=organization_name, plan=plan)
        if created is None:
            return
        _ = await send_notification(
            recipient.id,
            notification_kind=NotificationKind.ORGANIZATION_ADMISSION_PENDING,
            title="New organization admission request",
            body=f"{organization_name} ({created.email}) requested the {plan.name} plan.",
            link=REVIEW_LINK,
            ref_id=created.id,
        )


async def list_requests(
    reviewer_id: UUID, status: ApprovalStatus, *, limit: int, offset: int
) -> tuple[list[OrganizationAdmissionView], bool]:
    _ = await require_genesis_platform_admin(reviewer_id)
    rows = await admissions.list_by_status(status, limit=limit + 1, offset=offset)
    return await _views(rows[:limit]), len(rows) > limit


async def _approve(
    request: OrganizationAdmissionRecord, reviewer: UserRecord, note: str | None
) -> OrganizationAdmissionRecord:
    """Provision tenant, genesis org admin and the credential email intent inside the caller's transaction."""
    if request.organization_name is None:
        raise HTTPException(
            409, "This request has no organization name. Reject it and ask the requester to submit again."
        )
    password = new_password()
    try:
        provisioned = await create_tenant_with_org_admin(
            name=request.organization_name, admin_email=request.email, admin_password=password
        )
    except AdminEmailTakenError:
        raise HTTPException(409, "Admin email is already registered") from None
    except DomainClaimedError:
        raise HTTPException(409, "Email domain is already claimed") from None
    identity = provisioned.org_admin.identity
    if identity is None or identity.password_hash is None:
        raise RuntimeError("Provisioned organization admin has no credential")
    await emails.enqueue(
        NewCredentialEmail(
            request_id=request.id,
            identity_id=identity.id,
            to_email=request.email,
            credential_fingerprint=fingerprint(identity.password_hash),
            payload=seal(request.id, password),
        )
    )
    now = datetime.now(UTC)
    approved = await admissions.update(
        db_obj=request,
        obj_in={
            "status": ApprovalStatus.APPROVED,
            "reviewed_at": now,
            "reviewed_by": reviewer.id,
            "review_note": note,
            "provisioned_at": now,
            "provisioned_tenant_id": provisioned.tenant.id,
            "provisioned_admin_user_id": provisioned.org_admin.id,
        },
    )
    await write_admin_audit(
        actor=reviewer,
        action="tenant_create",
        target_type="tenant",
        target_id=provisioned.tenant.id,
        tenant_id=provisioned.tenant.id,
        changes={
            "name": field_change(None, provisioned.tenant.name),
            "org_admin_email": field_change(None, provisioned.admin_email),
        },
        details={"organization_admission_id": str(request.id), "org_admin_user_id": str(provisioned.org_admin.id)},
    )
    return approved


async def decide(
    request_id: UUID, reviewer_id: UUID, decision: OrganizationAdmissionDecisionRequest
) -> OrganizationAdmissionView:
    async with connection_ctx():
        _ = await require_genesis_platform_admin(reviewer_id)
        request = await admissions.get_locked(request_id)
        if request is None:
            raise HTTPException(404, "Organization admission request not found")
        reviewer = await require_genesis_platform_admin(reviewer_id)
        match decision.decision:
            case ApprovalAction.APPROVE:
                result = ApprovalStatus.APPROVED
            case ApprovalAction.REJECT:
                result = ApprovalStatus.REJECTED
            case unreachable:
                assert_never(unreachable)
        if request.status is not ApprovalStatus.PENDING:
            if request.status is not result:
                raise HTTPException(409, "Organization admission request already has a different decision")
            return (await _views([request]))[0]
        if result is ApprovalStatus.APPROVED:
            decided = await _approve(request, reviewer, decision.note)
        else:
            decided = await admissions.update(
                db_obj=request,
                obj_in={
                    "status": result,
                    "reviewed_at": datetime.now(UTC),
                    "reviewed_by": reviewer.id,
                    "review_note": decision.note,
                },
            )
        return (await _views([decided]))[0]


async def retry_credential_email(request_id: UUID, reviewer_id: UUID) -> OrganizationAdmissionView:
    """Re-queue the SAME encrypted intent after a failed delivery; never re-approves or rotates credentials."""
    async with connection_ctx():
        _ = await require_genesis_platform_admin(reviewer_id)
        request = await admissions.get_locked(request_id)
        if request is None:
            raise HTTPException(404, "Organization admission request not found")
        _ = await require_genesis_platform_admin(reviewer_id)
        state = (await emails.states([request.id])).get(request.id)
        if state is None:
            raise HTTPException(409, "This request has no credential email to retry")
        match state.status:
            case "sent":
                raise HTTPException(409, "The credential email was already accepted by the SMTP server")
            case "failed" if state.retryable:
                state = await emails.requeue(request.id, now=datetime.now(UTC)) or state
            case "failed" | "pending":
                pass
            case unreachable:
                assert_never(unreachable)
        return OrganizationAdmissionView(request, state)
