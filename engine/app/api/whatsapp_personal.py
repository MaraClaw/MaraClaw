from uuid import UUID

from fastapi import APIRouter, Depends, Response

from app.core.security import get_current_user
from app.records.user import UserRecord
from app.schemas.whatsapp_personal import PersonalRelink, PersonalStart, PersonalStatus
from app.services.channels import whatsapp_personal_control as control

router = APIRouter(prefix="/agents/{agent_id}/whatsapp-channel/personal", tags=["whatsapp"])


def private(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, private"


@router.get("", response_model=PersonalStatus)
async def get_personal(
    agent_id: UUID, response: Response, user: UserRecord = Depends(get_current_user)
) -> PersonalStatus:
    private(response)
    return await control.status(agent_id, user.id)


@router.post("/start", response_model=PersonalStatus, status_code=202)
async def start_personal(
    agent_id: UUID, data: PersonalStart, response: Response, user: UserRecord = Depends(get_current_user)
) -> PersonalStatus:
    private(response)
    return await control.start(agent_id, user.id, data.expected_generation)


@router.post("/relink", response_model=PersonalStatus, status_code=202)
async def relink_personal(
    agent_id: UUID, data: PersonalRelink, response: Response, user: UserRecord = Depends(get_current_user)
) -> PersonalStatus:
    private(response)
    return await control.start(agent_id, user.id, data.expected_generation, relink=True)


@router.delete("", response_model=PersonalStatus)
async def disconnect_personal(
    agent_id: UUID, expected_generation: UUID, response: Response, user: UserRecord = Depends(get_current_user)
) -> PersonalStatus:
    private(response)
    return await control.disconnect(agent_id, user.id, expected_generation)
