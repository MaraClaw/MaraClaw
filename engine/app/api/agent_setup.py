from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from app.core.security import get_current_user
from app.records.agent_setup import AgentSetupStatus
from app.records.user import UserRecord
from app.services.agent_setup import get_agent_setup_status, retry_agent_setup

router = APIRouter(prefix="/api/agents/{agent_id}/setup", tags=["agent-setup"])


@router.get("", response_model=AgentSetupStatus)
async def read_setup_status(
    agent_id: UUID, current_user: Annotated[UserRecord, Depends(get_current_user)]
) -> AgentSetupStatus:
    return await get_agent_setup_status(agent_id, current_user.id)


@router.post("/retry", response_model=AgentSetupStatus, status_code=202)
async def retry_setup(
    agent_id: UUID, current_user: Annotated[UserRecord, Depends(get_current_user)]
) -> AgentSetupStatus:
    return await retry_agent_setup(agent_id, current_user.id)
