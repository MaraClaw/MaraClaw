from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.core.domain.approvals import ApprovalStatus
from app.core.security import get_current_user
from app.records.user import UserRecord
from app.schemas.organization_admission import (
    OrganizationAdmissionAcknowledgment,
    OrganizationAdmissionDecisionRequest,
    OrganizationAdmissionItem,
    OrganizationAdmissionPage,
    OrganizationAdmissionRequest,
    OrganizationPlansResponse,
)
from app.services.organization_admissions import (
    OrganizationAdmissionView,
    decide,
    list_requests,
    retry_credential_email,
    submit_request,
)
from app.services.organization_plans import ORGANIZATION_PLANS

router = APIRouter(tags=["organization-admissions"])


def _item(view: OrganizationAdmissionView) -> OrganizationAdmissionItem:
    record, email = view.request, view.credential_email
    return OrganizationAdmissionItem(
        id=record.id,
        organization_name=record.organization_name,
        email=record.email,
        tier=record.tier,
        plan=record.plan,
        status=record.status,
        created_at=record.created_at,
        reviewed_at=record.reviewed_at,
        reviewed_by=record.reviewed_by,
        review_note=record.review_note,
        credential_email_status=email.status if email is not None else None,
        credential_email_error=email.last_error if email is not None else None,
    )


@router.get("/organization-plans", response_model=OrganizationPlansResponse)
async def organization_plans() -> OrganizationPlansResponse:
    return OrganizationPlansResponse(plans=list(ORGANIZATION_PLANS))


@router.post(
    "/organization-admissions",
    response_model=OrganizationAdmissionAcknowledgment,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_admission(data: OrganizationAdmissionRequest) -> OrganizationAdmissionAcknowledgment:
    await submit_request(data.email, data.organization_name, data.tier)
    return OrganizationAdmissionAcknowledgment()


@router.get("/admin/organization-admissions", response_model=OrganizationAdmissionPage)
async def review_admissions(
    status: ApprovalStatus = ApprovalStatus.PENDING,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: UserRecord = Depends(get_current_user),
) -> OrganizationAdmissionPage:
    records, has_more = await list_requests(user.id, status, limit=limit, offset=offset)
    return OrganizationAdmissionPage(items=[_item(record) for record in records], has_more=has_more)


@router.post("/admin/organization-admissions/{request_id}/decision", response_model=OrganizationAdmissionItem)
async def decide_admission(
    request_id: UUID, data: OrganizationAdmissionDecisionRequest, user: UserRecord = Depends(get_current_user)
) -> OrganizationAdmissionItem:
    return _item(await decide(request_id, user.id, data))


@router.post("/admin/organization-admissions/{request_id}/retry-email", response_model=OrganizationAdmissionItem)
async def retry_admission_email(
    request_id: UUID, user: UserRecord = Depends(get_current_user)
) -> OrganizationAdmissionItem:
    return _item(await retry_credential_email(request_id, user.id))
