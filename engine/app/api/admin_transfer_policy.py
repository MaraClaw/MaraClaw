from fastapi import APIRouter, Depends, Request

from app.core.security import require_role
from app.records.user import UserRecord
from app.schemas.transfer_policy import TransferPolicy
from app.services.transfer_policy import get_transfer_policy, update_transfer_policy

router = APIRouter(prefix="/transfer-policy")


@router.get("", response_model=TransferPolicy)
async def read_transfer_policy(
    _current_user: UserRecord = Depends(require_role("platform_admin")),
) -> TransferPolicy:
    return await get_transfer_policy()


@router.put("", response_model=TransferPolicy)
async def replace_transfer_policy(
    body: TransferPolicy,
    request: Request,
    current_user: UserRecord = Depends(require_role("platform_admin")),
) -> TransferPolicy:
    ip_address = request.client.host if request.client is not None else None
    return await update_transfer_policy(body, current_user, ip_address)
