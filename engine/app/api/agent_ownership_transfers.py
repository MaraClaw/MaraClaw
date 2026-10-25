from fastapi import APIRouter, Depends

from app.core.security import get_current_user
from app.records.user import UserRecord
from app.schemas.agent_ownership_transfer import OwnershipTransferRequest, OwnershipTransferResponse
from app.services.agent_ownership_transfer import OwnershipChange, apply_ownership_change

router = APIRouter(prefix="/agent-ownership-transfers", tags=["agent-ownership-transfers"])


@router.post("", response_model=OwnershipTransferResponse)
async def transfer_ownership(
    data: OwnershipTransferRequest, current_user: UserRecord = Depends(get_current_user)
) -> OwnershipTransferResponse:
    return await apply_ownership_change(
        OwnershipChange(current_user, data.from_user_id, data.to_user_id, tuple(data.agent_ids), org_admin_only=True)
    )
