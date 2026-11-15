from uuid import UUID

from fastapi import APIRouter, Depends, Response

from app.core.security import get_current_user
from app.records.user import UserRecord
from app.schemas.zalo_personal import ZaloPersonalRelink, ZaloPersonalStart, ZaloPersonalStatus
from app.services.channels import zalo_personal_control as control

router = APIRouter(prefix="/agents/{agent_id}/zalo-channel/personal", tags=["zalo"])


@router.get("", response_model=ZaloPersonalStatus)
async def get_personal(
    agent_id: UUID, response: Response, user: UserRecord = Depends(get_current_user)
) -> ZaloPersonalStatus:
    response.headers["Cache-Control"] = "no-store, private"
    return await control.status(agent_id, user.id)


@router.post("/start", response_model=ZaloPersonalStatus, status_code=202)
async def start_personal(
    agent_id: UUID, data: ZaloPersonalStart, response: Response, user: UserRecord = Depends(get_current_user)
) -> ZaloPersonalStatus:
    response.headers["Cache-Control"] = "no-store, private"
    return await control.start(agent_id, user.id, data.expected_generation)


@router.post("/relink", response_model=ZaloPersonalStatus, status_code=202)
async def relink_personal(
    agent_id: UUID, data: ZaloPersonalRelink, response: Response, user: UserRecord = Depends(get_current_user)
) -> ZaloPersonalStatus:
    response.headers["Cache-Control"] = "no-store, private"
    return await control.start(agent_id, user.id, data.expected_generation, relink=True)


@router.delete("", response_model=ZaloPersonalStatus, status_code=202)
async def disconnect_personal(
    agent_id: UUID, expected_generation: UUID, response: Response, user: UserRecord = Depends(get_current_user)
) -> ZaloPersonalStatus:
    response.headers["Cache-Control"] = "no-store, private"
    return await control.disconnect(agent_id, user.id, expected_generation)
