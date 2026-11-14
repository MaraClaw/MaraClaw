"""Public organization admission requests and their genesis platform-admin review.

A visitor asks for a server quote (``quote_request``), pays it by bank transfer and submits the request with the
payment evidence (``submit_request``); only the first period (month or year) is collected. The genesis platform admin
reviews the evidence. Approving a named request provisions the organization in the same transaction: tenant, genesis
org admin and a durable, encrypted initial-credential email intent. The email is sent after commit by
``organization_admission_email_worker`` over the platform SMTP.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, assert_never
from uuid import UUID, uuid4

from fastapi import HTTPException

from app.core.domain.approvals import ApprovalAction, ApprovalStatus
from app.core.notification_kinds import NotificationKind
from app.core.security import load_user_principal
from app.dao.identity_dao import identity_dao
from app.dao.organization_admission_dao import REFERENCE_UNIQUE_CONSTRAINT, organization_admission_dao as admissions
from app.dao.organization_admission_email_dao import organization_admission_email_dao as emails
from app.dao.tenant_email_domain_dao import tenant_email_domain_dao
from app.dao.user_dao import user_dao
from app.db.errors import UniqueViolationError
from app.db.session import connection_ctx
from app.records.capacity_order import CapacityEvidence, CapacityRecipient, new_reference
from app.records.organization_admission import (
    ADMISSION_REFERENCE_PREFIX,
    AdmissionPayment,
    OrganizationAdmissionRecord,
)
from app.records.organization_admission_email import CredentialEmailState, NewCredentialEmail
from app.records.user import UserRecord
from app.schemas.capacity import CapacityRecipientOut
from app.schemas.organization_admission import (
    OrganizationAdmissionDecisionRequest,
    OrganizationAdmissionQuote,
    OrganizationAdmissionQuoteRequest,
    OrganizationAdmissionQuoteTerms,
)
from app.services.admin_audit import field_change, write_admin_audit
from app.services.admin_provisioning import is_genesis_platform_admin
from app.services.capacity_evidence import (
    MAX_EVIDENCE_BYTES,
    EvidenceFile,
    admission_storage_key,
    discard_evidence,
    safe_filename,
    sha256_hex,
    sniff_evidence,
    store_evidence,
)
from app.services.capacity_notifications import format_vnd
from app.services.capacity_payment_settings import load_payment_settings
from app.services.capacity_pricing import round_up_vnd
from app.services.coded_errors import coded_error, conflict
from app.services.notification_service import send_notification
from app.services.org_membership import DomainClaimedError, email_domain
from app.services.organization_admission_credentials import fingerprint, new_password, seal
from app.services.organization_admission_quote_token import (
    QuoteTokenExpiredError,
    QuoteTokenInvalidError,
    mint_quote_token,
    verify_quote_token,
)
from app.services.organization_admission_rate_limit import allow_submission
from app.services.organization_plans import first_period_price_usd, plan_for
from app.services.storage_runtime.facade import get_storage_backend
from app.services.tenant_provisioning import AdminEmailTakenError, create_tenant_with_org_admin

REVIEW_LINK = "/organization-admissions"
QUOTE_VALIDITY: Final = timedelta(days=7)


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


async def _require_genesis_recipient() -> UserRecord:
    recipient = await user_dao.genesis_platform_admin()
    if recipient is None or not recipient.is_active:
        raise HTTPException(503, "Organization admissions are temporarily unavailable")
    return recipient


async def _refuse_known_conflicts(email: str) -> None:
    """Refuse what approval would refuse (or the pending-email index would), so no payment is taken for it."""
    if await admissions.has_pending(email):
        raise conflict("admission_already_pending", "A request for this email address is already waiting for review.")
    if await identity_dao.get_by_email(email) is not None:
        raise conflict("email_registered", "This email address already belongs to an account. Sign in instead.")
    domain = email_domain(email)
    if domain is not None and await tenant_email_domain_dao.get_by_domain(domain) is not None:
        raise conflict("domain_claimed", "This email domain already belongs to another organization.")


async def quote_request(
    data: OrganizationAdmissionQuoteRequest, *, now: datetime | None = None
) -> OrganizationAdmissionQuote:
    """Price a request on the server and sign the price; nothing is stored.

    The amount is the plan's monthly price or its annual price, unprorated (the plan starts on approval), converted at
    the genesis admin's USD/VND rate and rounded up to the next thousand dong.
    """
    settings = await load_payment_settings()
    if settings is None:
        raise conflict("payments_not_configured", "Organization requests are temporarily unavailable. Try again later.")
    async with connection_ctx():
        _ = await _require_genesis_recipient()
        await _refuse_known_conflicts(data.email)
    plan = plan_for(data.tier)
    amount_usd = first_period_price_usd(plan, data.billing)
    terms = OrganizationAdmissionQuoteTerms(
        organization_name=data.organization_name,
        email=data.email,
        phone=data.phone,
        tier=data.tier,
        billing=data.billing,
        amount_usd=amount_usd,
        usd_vnd_rate=settings.usd_vnd_rate,
        amount_vnd=round_up_vnd(amount_usd, settings.usd_vnd_rate),
        recipient=CapacityRecipientOut.model_validate(settings.recipient.as_json()),
        reference=new_reference(ADMISSION_REFERENCE_PREFIX),
        expires_at=(now or datetime.now(UTC)) + QUOTE_VALIDITY,
    )
    return OrganizationAdmissionQuote(
        organization_name=terms.organization_name,
        email=terms.email,
        phone=terms.phone,
        tier=terms.tier,
        billing=terms.billing,
        amount_usd=terms.amount_usd,
        usd_vnd_rate=terms.usd_vnd_rate,
        amount_vnd=terms.amount_vnd,
        recipient=terms.recipient,
        reference=terms.reference,
        expires_at=terms.expires_at,
        plan=plan,
        quote_token=mint_quote_token(terms),
    )


def verify_submission_token(token: str, *, now: datetime | None = None) -> OrganizationAdmissionQuoteTerms:
    """The quote a submit token binds; 400 ``quote_invalid`` or 410 ``quote_expired`` otherwise."""
    try:
        return verify_quote_token(token, now=now or datetime.now(UTC))
    except QuoteTokenExpiredError:
        raise coded_error(
            410, "quote_expired", "This price quote has expired. Start again from the pricing page."
        ) from None
    except QuoteTokenInvalidError:
        raise coded_error(
            400, "quote_invalid", "This price quote is not valid. Start again from the pricing page."
        ) from None


async def submit_request(
    terms: OrganizationAdmissionQuoteTerms,
    *,
    data: bytes,
    filename: str | None,
    client_ip: str | None,
    now: datetime | None = None,
) -> str:
    """Store the payment evidence and one pending request holding the quote's snapshot; returns its reference.

    ``terms`` come from ``verify_submission_token``: nothing the client sent outside the token is used. Evidence that
    no committed row references is deleted again.
    """
    kind = sniff_evidence(data)
    if kind is None:
        raise HTTPException(415, "Payment evidence must be a PNG, JPEG or WebP image or a PDF")
    if not await allow_submission(client_ip, now=now or datetime.now(UTC)):
        raise coded_error(429, "too_many_requests", "Too many requests. Try again later.")
    async with connection_ctx():
        _ = await _require_genesis_recipient()
        await _refuse_known_conflicts(terms.email)
    plan = plan_for(terms.tier)
    request_id = uuid4()
    storage = get_storage_backend()
    key = await store_evidence(storage, lambda: admission_storage_key(request_id, kind), kind, data)
    evidence = CapacityEvidence(
        storage_key=key,
        content_type=kind.content_type,
        filename=safe_filename(filename, kind),
        size_bytes=len(data),
        sha256=sha256_hex(data),
    )
    payment = AdmissionPayment(
        billing=terms.billing,
        amount_usd=terms.amount_usd,
        usd_vnd_rate=terms.usd_vnd_rate,
        amount_vnd=terms.amount_vnd,
        recipient=CapacityRecipient.from_json(terms.recipient.model_dump()),
        reference=terms.reference,
    )
    try:
        async with connection_ctx():
            recipient = await _require_genesis_recipient()
            created = await admissions.insert_pending(
                request_id=request_id,
                email=terms.email,
                phone=terms.phone,
                organization_name=terms.organization_name,
                plan=plan,
                payment=payment,
                evidence=evidence,
            )
            if created is None:
                raise conflict(
                    "admission_already_pending", "A request for this email address is already waiting for review."
                )
            _ = await send_notification(
                recipient.id,
                notification_kind=NotificationKind.ORGANIZATION_ADMISSION_PENDING,
                title="New organization admission request",
                body=(
                    f"{terms.organization_name} ({created.email}) requested the {plan.name} plan "
                    f"({terms.billing.value}) and sent payment evidence for {format_vnd(terms.amount_vnd)}, "
                    f"reference {terms.reference}."
                ),
                link=REVIEW_LINK,
                ref_id=created.id,
            )
    except BaseException as exc:
        await discard_evidence(storage, key)
        if isinstance(exc, UniqueViolationError) and exc.constraint == REFERENCE_UNIQUE_CONSTRAINT:
            raise conflict("quote_already_used", "This request was already submitted.") from None
        raise
    return terms.reference


async def read_evidence(reviewer_id: UUID, request_id: UUID) -> EvidenceFile:
    """The stored payment evidence of a request, for the genesis platform admin."""
    async with connection_ctx():
        _ = await require_genesis_platform_admin(reviewer_id)
        request = await admissions.get(request_id)
    if request is None:
        raise HTTPException(404, "Organization admission request not found")
    try:
        data = await get_storage_backend().read_bytes_bounded(request.evidence.storage_key, MAX_EVIDENCE_BYTES)
    except FileNotFoundError:
        raise HTTPException(404, "The payment evidence file is missing") from None
    return EvidenceFile(data=data, content_type=request.evidence.content_type, filename=request.evidence.filename)


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
            tier=request.tier,
            name=request.organization_name,
            phone=request.phone,
            admin_email=request.email,
            admin_password=password,
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
            "phone": field_change(None, provisioned.tenant.phone),
            "org_admin_email": field_change(None, provisioned.admin_email),
        },
        details={
            "organization_admission_id": str(request.id),
            "org_admin_user_id": str(provisioned.org_admin.id),
            "payment_reference": request.payment_reference,
        },
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
