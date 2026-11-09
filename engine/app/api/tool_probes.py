from typing import assert_never

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, TypeAdapter

from app.core.security import get_current_user
from app.records.user import UserRecord
from app.schemas.operator_response_tools import MCPTestResponse
from app.schemas.tool_management import SavedToolProbe
from app.services.email_service import EmailConfig, EmailConnectionResult
from app.services.tool_config_operations import reauthorize_saved_probe, saved_probe_config
from app.services.tool_management_auth import ToolActor, get_tool_actor

router = APIRouter()


class MCPTestRequest(BaseModel):
    server_url: str
    api_key: str | None = None


class EmailTestRequest(BaseModel):
    config: EmailConfig


@router.post("/test-mcp")
async def check_mcp_connection(
    data: MCPTestRequest | SavedToolProbe,
    current_user: UserRecord = Depends(get_current_user),
    actor: ToolActor = Depends(get_tool_actor),
) -> MCPTestResponse:
    _ = current_user
    from app.core.outbound_policy import OutboundPolicyError, validate_public_url
    from app.services.mcp_client import MCPClient

    match data:
        case SavedToolProbe() as saved:
            resolved = await saved_probe_config(saved.saved, saved.operations, actor)
            draft_url = resolved.config.get("mcp_server_url")
            url = draft_url if isinstance(draft_url, str) else resolved.tool.mcp_server_url
            key = resolved.config.get("api_key")
            api_key = key if isinstance(key, str) else None
            if url in (None, ""):
                raise HTTPException(422, "Saved tool has no MCP server URL")
        case MCPTestRequest():
            saved = None
            url, api_key = data.server_url, data.api_key
        case unreachable:
            assert_never(unreachable)
    try:
        server_url = await validate_public_url(url)
        client = MCPClient(server_url, api_key=api_key or None)
        tools = await client.list_tools()
        if saved is not None:
            await reauthorize_saved_probe(saved.saved, actor)
        return {"ok": True, "tools": [] if saved is not None else tools}
    except OutboundPolicyError as exc:
        raise HTTPException(status_code=400, detail="MCP server URL is not allowed") from exc
    except HTTPException:
        raise
    except Exception as exc:
        error = "MCP connection failed" if saved is not None else str(exc).replace(url, "[MCP server]")
        if api_key not in (None, ""):
            error = error.replace(api_key, "[credential]")
        return {"ok": False, "error": error[:300]}


@router.post("/test-email")
async def check_email_connection(
    data: EmailTestRequest | SavedToolProbe,
    current_user: UserRecord = Depends(get_current_user),
    actor: ToolActor = Depends(get_tool_actor),
) -> EmailConnectionResult:
    _ = current_user
    from app.services.email_service import test_connection

    match data:
        case SavedToolProbe() as saved:
            resolved = await saved_probe_config(saved.saved, saved.operations, actor)
            config = TypeAdapter(EmailConfig).validate_python(resolved.config)
        case EmailTestRequest():
            saved = None
            config = data.config
        case unreachable:
            assert_never(unreachable)
    try:
        result = await test_connection(config)
        if saved is not None:
            await reauthorize_saved_probe(saved.saved, actor)
            secret = config.get("auth_code")
            if secret not in (None, ""):
                if "error" in result:
                    result["error"] = result["error"].replace(secret, "[credential]")
                if "imap" in result:
                    result["imap"] = result["imap"].replace(secret, "[credential]")
                if "smtp" in result:
                    result["smtp"] = result["smtp"].replace(secret, "[credential]")
        return result
    except HTTPException:
        raise
    except Exception as exc:
        return {"ok": False, "error": "Email connection failed" if saved is not None else str(exc)[:300]}
