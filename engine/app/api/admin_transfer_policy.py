from fastapi import APIRouter, Depends, Request

from app.core.client_address import get_client_ip
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
    return await update_transfer_policy(body, current_user, await get_client_ip(request))
