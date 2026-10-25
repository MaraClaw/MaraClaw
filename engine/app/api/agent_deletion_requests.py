from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.core.security import get_current_user
from app.records.agent_deletion_request import DeletionStatus
from app.records.user import UserRecord
from app.schemas.agent_deletion_request import DeletionDetail, DeletionPage, DeletionRequestCreate
from app.services import agent_deletion_requests as service
from app.services.agent_deletion_approval import approve
from app.services.agent_deletion_views import detail

router = APIRouter(prefix="/agent-deletion-requests", tags=["agent-deletion-requests"])


@router.post("", response_model=DeletionDetail, status_code=201)
async def create_request(
    data: DeletionRequestCreate, current_user: UserRecord = Depends(get_current_user)
) -> DeletionDetail:
    return await detail(await service.submit(current_user, data), current_user.id)


@router.get("", response_model=DeletionPage)
async def list_requests(
    status: DeletionStatus | None = None,
    agent_id: UUID | None = None,
    limit: int = Query(50, ge=1, le=100),
    cursor: str | None = Query(None, max_length=2048),
    current_user: UserRecord = Depends(get_current_user),
) -> DeletionPage:
    return await service.list_page(current_user, service.DeletionQuery(status, agent_id, limit, cursor))


@router.get("/{request_id}", response_model=DeletionDetail)
async def get_request(request_id: UUID, current_user: UserRecord = Depends(get_current_user)) -> DeletionDetail:
    return await service.get_detail(request_id, current_user)


@router.post("/{request_id}/approve", response_model=DeletionDetail)
async def approve_request(request_id: UUID, current_user: UserRecord = Depends(get_current_user)) -> DeletionDetail:
    return await approve(request_id, current_user)


@router.post("/{request_id}/cancel", response_model=DeletionDetail)
async def cancel_request(request_id: UUID, current_user: UserRecord = Depends(get_current_user)) -> DeletionDetail:
    return await detail(await service.withdraw(request_id, current_user), current_user.id)
