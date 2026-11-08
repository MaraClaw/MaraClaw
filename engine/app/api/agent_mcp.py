from collections.abc import Callable, Coroutine
from typing import Annotated, override
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from app.core.security import get_current_user
from app.records.user import UserRecord
from app.schemas.agent_mcp import MCPServerCreate, MCPServerInventory, MCPServerName, MCPServerPatch, MCPServerSummary
from app.services import agent_mcp


class MCPConfigRoute(APIRoute):
    @override
    def get_route_handler(self) -> Callable[[Request], Coroutine[object, object, Response]]:
        handler = super().get_route_handler()

        async def safe_handler(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError:
                return JSONResponse(status_code=422, content={"detail": "mcp_input_invalid"})

        return safe_handler


router = APIRouter(prefix="/api/agents/{agent_id}/mcp/servers", tags=["agent-mcp"], route_class=MCPConfigRoute)
type CurrentUser = Annotated[UserRecord, Depends(get_current_user)]


@router.get("", response_model=MCPServerInventory)
async def list_servers(agent_id: UUID, current_user: CurrentUser) -> MCPServerInventory:
    return await agent_mcp.list_servers(agent_id, current_user.id)


@router.post("", response_model=MCPServerSummary, status_code=201)
async def add_server(
    agent_id: UUID, payload: MCPServerCreate, current_user: CurrentUser, response: Response
) -> MCPServerSummary:
    summary, applied = await agent_mcp.add_server(agent_id, current_user.id, payload)
    response.headers["X-MCP-Sync"] = "applied" if applied else "pending"
    return summary


@router.patch("/{name}", response_model=MCPServerSummary)
async def configure_server(
    agent_id: UUID, name: MCPServerName, payload: MCPServerPatch, current_user: CurrentUser, response: Response
) -> MCPServerSummary:
    summary, applied = await agent_mcp.configure_server(agent_id, current_user.id, name, payload)
    response.headers["X-MCP-Sync"] = "applied" if applied else "pending"
    return summary


@router.delete("/{name}", status_code=204)
async def delete_server(agent_id: UUID, name: MCPServerName, current_user: CurrentUser) -> Response:
    applied = await agent_mcp.delete_server(agent_id, current_user.id, name)
    return Response(status_code=204, headers={"X-MCP-Sync": "applied" if applied else "pending"})
