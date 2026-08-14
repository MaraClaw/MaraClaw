"""Atlassian Rovo MCP Channel API routes.

Provides per-agent Atlassian integration configuration.
Unlike Slack/Discord (messaging channels), Atlassian is a tool-access channel:
the agent uses Jira, Confluence, and Compass via the Atlassian Rovo MCP server.
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.core.json_types import JsonValue
from app.core.permissions import check_agent_access, is_agent_creator
from app.core.security import get_current_user
from app.dao.channel_config_dao import channel_config_dao
from app.records.channel_config import ChannelConfigRecord
from app.records.user import UserRecord
from app.services.atlassian_mcp_tools import (
    ATLASSIAN_MCP_URL,
    _preview_atlassian_tools,
    _sync_atlassian_tools_for_agent,
)

router = APIRouter(tags=["atlassian"])


# ─── Config CRUD ────────────────────────────────────────


@router.post("/agents/{agent_id}/atlassian-channel", status_code=201)
async def configure_atlassian_channel(
    agent_id: uuid.UUID, data: dict[str, str], current_user: UserRecord = Depends(get_current_user)
):
    """Configure Atlassian Rovo MCP for an agent.

    Required field: api_key (Bearer token starting with ATSTT, or Basic base64(email:token)).
    Optional: cloud_id (Atlassian cloud site ID for multi-site setups).
    """
    agent, _ = await check_agent_access(current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can configure channel")

    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        raise HTTPException(status_code=422, detail="api_key is required")

    cloud_id = (data.get("cloud_id") or "").strip()

    from app.config import get_settings
    from app.core.security import encrypt_data

    encrypted_key = encrypt_data(api_key, get_settings().SECRET_KEY)

    existing = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="atlassian")
    if existing:
        config = await channel_config_dao.update(
            db_obj=existing,
            obj_in={
                "app_secret": encrypted_key,
                "is_configured": True,
                "extra_config": {**(existing.extra_config or {}), "cloud_id": cloud_id},
            },
        )
        from app.api.background_tasks import schedule_background_task

        _ = schedule_background_task(_sync_atlassian_tools_for_agent(agent_id, api_key), "sync Atlassian tools")
        return _serialize(config or existing)

    config = await channel_config_dao.create(
        obj_in={
            "agent_id": agent_id,
            "channel_type": "atlassian",
            "app_id": "atlassian",
            "app_secret": encrypted_key,
            "is_configured": True,
            "extra_config": {"cloud_id": cloud_id},
        }
    )
    from app.api.background_tasks import schedule_background_task

    _ = schedule_background_task(_sync_atlassian_tools_for_agent(agent_id, api_key), "sync Atlassian tools")
    return _serialize(config)


@router.get("/agents/{agent_id}/atlassian-channel")
async def get_atlassian_channel(agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)):
    _ = await check_agent_access(current_user, agent_id)
    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="atlassian")
    if not config:
        raise HTTPException(status_code=404, detail="Atlassian not configured")
    return _serialize(config)


@router.delete("/agents/{agent_id}/atlassian-channel", status_code=204)
async def delete_atlassian_channel(agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)):
    agent, _ = await check_agent_access(current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can remove channel")
    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="atlassian")
    if not config:
        raise HTTPException(status_code=404, detail="Atlassian not configured")
    _ = await channel_config_dao.delete(id=config.id)


@router.post("/agents/{agent_id}/atlassian-channel/test")
async def check_atlassian_channel(
    agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)
) -> dict[str, Any]:
    """Test connectivity to Atlassian Rovo MCP and list available tools."""
    _ = await check_agent_access(current_user, agent_id)
    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="atlassian")
    if not config or not config.app_secret:
        raise HTTPException(status_code=400, detail="Atlassian not configured")

    from app.services.mcp_client import MCPClient

    try:
        client = MCPClient(ATLASSIAN_MCP_URL, api_key=config.app_secret)
        tools = await client.list_tools()
        tool_previews = _preview_atlassian_tools(tools)
        return {
            "ok": True,
            "tool_count": len(tools),
            "tools": tool_previews,
            "message": f"✅ Connected to Atlassian Rovo MCP - {len(tools)} tools available",
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def _serialize(config: ChannelConfigRecord) -> dict[str, JsonValue]:
    return {
        "id": str(config.id),
        "agent_id": str(config.agent_id),
        "channel_type": config.channel_type,
        "is_configured": config.is_configured,
        "is_connected": config.is_connected,
        "cloud_id": (config.extra_config or {}).get("cloud_id", ""),
        "extra_config": config.extra_config or {},
        "created_at": config.created_at.isoformat() if config.created_at else None,
    }


async def get_atlassian_api_key_for_agent(agent_id: uuid.UUID, db: object | None = None) -> str | None:
    """Return the configured Atlassian API key for the given agent, or None."""
    from app.config import get_settings
    from app.core.security import decrypt_data

    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="atlassian")
    if not config or not config.is_configured or not config.app_secret:
        return None

    try:
        return decrypt_data(config.app_secret, get_settings().SECRET_KEY)
    except Exception:
        return config.app_secret
