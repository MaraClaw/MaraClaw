from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from app.core.security import get_current_user
from app.records.user import UserRecord
from app.schemas.agent_lifecycle import AgentLifecycleOut
from app.services.agent_lifecycle_projection import get_agent_lifecycle

router = APIRouter(prefix="/api/agents/{agent_id}/lifecycle", tags=["agent-lifecycle"])


@router.get("", response_model=AgentLifecycleOut)
async def read_agent_lifecycle(
    agent_id: UUID, current_user: Annotated[UserRecord, Depends(get_current_user)]
) -> AgentLifecycleOut:
    return await get_agent_lifecycle(agent_id, current_user.id)
