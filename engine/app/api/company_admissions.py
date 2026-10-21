from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.security import get_current_user
from app.dao.company_admission_dao import company_admission_dao as admissions
from app.records.company_admission import AdmissionStatus
from app.records.user import UserRecord
from app.schemas.company_admission import AdmissionDecision, RequestDetail, RequestSummary
from app.services.company_admission_decisions import decide
from app.services.company_admissions import detail, require_genesis, summarize

router = APIRouter(prefix="/company-admissions", tags=["company-admissions"])


@router.get("/mine", response_model=list[RequestSummary])
async def mine(
    limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0), user: UserRecord = Depends(get_current_user)
):
    return [await summarize(item) for item in await admissions.list_mine(user.id, limit=limit, offset=offset)]


@router.get("/review", response_model=list[RequestDetail])
async def review(
    status: AdmissionStatus = AdmissionStatus.PENDING,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: UserRecord = Depends(get_current_user),
):
    reviewer = await require_genesis(user.id)
    if reviewer.tenant_id is None:
        raise HTTPException(403, "Company membership required")
    return [
        await detail(item)
        for item in await admissions.list_review(reviewer.tenant_id, status, limit=limit, offset=offset)
    ]


@router.get("/{request_id}", response_model=RequestDetail)
async def get_request(request_id: UUID, user: UserRecord = Depends(get_current_user)):
    request = await admissions.get(request_id)
    if request is None:
        raise HTTPException(404, "Admission request not found")
    if user.id != request.applicant_user_id:
        await require_genesis(user.id, request.target_tenant_id)
    return await detail(request)


@router.post("/{request_id}/decision", response_model=RequestDetail)
async def decision(request_id: UUID, data: AdmissionDecision, user: UserRecord = Depends(get_current_user)):
    return await detail(await decide(request_id, user.id, data))
