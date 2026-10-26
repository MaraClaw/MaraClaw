from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from app.core.security import get_current_user
from app.records.user import UserRecord
from app.schemas.agent_setup_mcp import AgentSetupMCPRetry, AgentSetupMCPStatus
from app.services.agent_setup_mcp import retry_agent_setup_mcp

router = APIRouter(prefix="/api/agents/{agent_id}/setup", tags=["agent-setup"])


@router.post("/mcp-retry", response_model=AgentSetupMCPStatus, status_code=202)
async def retry_optional_mcp(
    agent_id: UUID, body: AgentSetupMCPRetry, current_user: Annotated[UserRecord, Depends(get_current_user)]
) -> AgentSetupMCPStatus:
    return await retry_agent_setup_mcp(agent_id, current_user.id, body.server_id)
