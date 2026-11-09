"""Additive configuration routes, alongside legacy replacement routes."""

from uuid import UUID

from fastapi import APIRouter, Depends

from app.schemas.operator_response_tools import ToolAcknowledgement
from app.schemas.tool_management import CompanyConfigOperations, ConfigOperations, SavedToolReference
from app.services.tool_config_operations import patch_stored_config
from app.services.tool_management_auth import ToolActor, get_tool_actor

router = APIRouter()


@router.patch("/{tool_id}")
async def patch_company_tool_config(
    tool_id: UUID, data: CompanyConfigOperations, actor: ToolActor = Depends(get_tool_actor)
) -> ToolAcknowledgement:
    reference = SavedToolReference(tool_id=tool_id, tenant_id=data.tenant_id)
    await patch_stored_config(reference, ConfigOperations(set=data.set, unset=data.unset), actor)
    return {"ok": True}


@router.patch("/agents/{agent_id}/tool-config/{tool_id}")
async def patch_agent_tool_config(
    agent_id: UUID, tool_id: UUID, data: ConfigOperations, actor: ToolActor = Depends(get_tool_actor)
) -> ToolAcknowledgement:
    await patch_stored_config(SavedToolReference(tool_id=tool_id, agent_id=agent_id), data, actor)
    return {"ok": True}
