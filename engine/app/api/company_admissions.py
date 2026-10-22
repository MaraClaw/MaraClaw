from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.security import get_current_user
from app.dao.company_admission_dao import company_admission_dao as admissions
from app.records.company_admission import AdmissionStatus
from app.records.user import UserRecord
from app.schemas.company_admission import AdmissionDecision, RequestDetail, RequestSummary, ReviewPage
from app.services.company_admission_cursor import ReviewCursor
from app.services.company_admission_decisions import decide
from app.services.company_admissions import detail, require_genesis, summarize, withdraw_request

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


@router.get("/review/page", response_model=ReviewPage)
async def review_page(
    status: AdmissionStatus = AdmissionStatus.PENDING,
    limit: int = Query(50, ge=1, le=100),
    cursor: str | None = Query(None),
    user: UserRecord = Depends(get_current_user),
) -> ReviewPage:
    reviewer = await require_genesis(user.id)
    if reviewer.tenant_id is None:
        raise HTTPException(403, "Company membership required")
    position = ReviewCursor.decode(cursor) if cursor is not None else None
    if position is not None and (position.tenant_id != reviewer.tenant_id or position.status != status):
        raise HTTPException(400, "Invalid cursor")
    rows = await admissions.list_review_page(
        reviewer.tenant_id,
        status,
        limit=limit,
        before=(position.created_at, position.id) if position is not None else None,
    )
    items = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = items[-1]
        next_cursor = ReviewCursor(
            version=1, tenant_id=reviewer.tenant_id, status=status, created_at=last.created_at, id=last.id
        ).encode()
    return ReviewPage(items=[await detail(item) for item in items], next_cursor=next_cursor)


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


@router.post("/{request_id}/withdraw", response_model=RequestDetail)
async def withdraw(request_id: UUID, user: UserRecord = Depends(get_current_user)):
    return await detail(await withdraw_request(request_id, user.id))
