from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.security import get_current_user
from app.dao.company_transfer_dao import company_transfer_dao as transfers
from app.records.company_transfer import TransferReviewView
from app.records.user import UserRecord
from app.schemas.company_transfer import PaymentDecision, TransferQuote, TransferReviewPage, TransferSummary
from app.services.company_transfer_cursor import TransferReviewCursor
from app.services.company_transfer_guards import quote as transfer_quote
from app.services.company_transfer_review import decide_payment, require_payment_reviewer, withdraw_transfer

router = APIRouter()


@router.get("/transfer-quote", response_model=TransferQuote)
async def quote(user: UserRecord = Depends(get_current_user)) -> TransferQuote:
    return await transfer_quote(user)


@router.get("/transfers/mine", response_model=list[TransferSummary])
async def mine(
    limit: int = Query(50, ge=1, le=100), user: UserRecord = Depends(get_current_user)
) -> list[TransferSummary]:
    return [TransferSummary.model_validate(row) for row in await transfers.list_requests(user.id, limit)]


@router.get("/transfers/review", response_model=list[TransferSummary])
async def review(
    limit: int = Query(50, ge=1, le=100), user: UserRecord = Depends(get_current_user)
) -> list[TransferSummary]:
    await require_payment_reviewer(user.id)
    return [TransferSummary.model_validate(row) for row in await transfers.list_requests(None, limit)]


@router.get("/transfers/review/page", response_model=TransferReviewPage)
async def review_page(
    view: TransferReviewView = TransferReviewView.PENDING,
    limit: int = Query(50, ge=1, le=100),
    cursor: str | None = Query(None, max_length=2048),
    user: UserRecord = Depends(get_current_user),
) -> TransferReviewPage:
    await require_payment_reviewer(user.id)
    position = TransferReviewCursor.decode(cursor, view) if cursor is not None else None
    rows = await transfers.list_review_page(
        view, limit=limit, before=(position.created_at, position.id) if position is not None else None
    )
    items = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = items[-1]
        next_cursor = TransferReviewCursor(version=1, view=view, created_at=last.created_at, id=last.id).encode()
    return TransferReviewPage(items=[TransferSummary.model_validate(row) for row in items], next_cursor=next_cursor)


@router.get("/transfers/{request_id}", response_model=TransferSummary)
async def get_request(request_id: UUID, user: UserRecord = Depends(get_current_user)) -> TransferSummary:
    request = await transfers.get(request_id)
    if request is None:
        raise HTTPException(404, "Transfer request not found")
    if request.applicant_user_id != user.id:
        await require_payment_reviewer(user.id)
    return TransferSummary.model_validate(request)


@router.post("/transfers/{request_id}/decision", response_model=TransferSummary)
async def decision(
    request_id: UUID, data: PaymentDecision, user: UserRecord = Depends(get_current_user)
) -> TransferSummary:
    return TransferSummary.model_validate(await decide_payment(request_id, user.id, data))


@router.post("/transfers/{request_id}/withdraw", response_model=TransferSummary)
async def withdraw(request_id: UUID, user: UserRecord = Depends(get_current_user)) -> TransferSummary:
    return TransferSummary.model_validate(await withdraw_transfer(request_id, user.id))
