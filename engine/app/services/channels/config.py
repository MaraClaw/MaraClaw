"""Shared channel_config CRUD helpers for agent-scoped chat integrations."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import HTTPException

from app.core.permissions import check_agent_access, is_agent_creator
from app.dao.channel_config_dao import channel_config_dao
from app.records.channel_config import ChannelConfigRecord
from app.records.user import UserRecord
from app.services.channels.types import normalize_channel_type


async def require_channel_creator(current_user: UserRecord, agent_id: uuid.UUID) -> Any:
    agent, _ = await check_agent_access(current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can configure channel")
    return agent


async def get_channel_config(agent_id: uuid.UUID, channel_type: str) -> ChannelConfigRecord | None:
    stored = normalize_channel_type(channel_type) or channel_type
    return await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type=stored)


async def require_channel_config(agent_id: uuid.UUID, channel_type: str) -> ChannelConfigRecord:
    config = await get_channel_config(agent_id, channel_type)
    if not config:
        raise HTTPException(status_code=404, detail=f"{channel_type} not configured")
    return config


async def upsert_channel_config(
    *,
    agent_id: uuid.UUID,
    channel_type: str,
    app_id: str,
    app_secret: str | None = None,
    encrypt_key: str | None = None,
    verification_token: str | None = None,
    extra_config: dict[str, Any] | None = None,
    is_configured: bool = True,
) -> ChannelConfigRecord:
    """Create or update a channel_config row for an agent."""
    stored = normalize_channel_type(channel_type) or channel_type
    existing = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type=stored)
    payload: dict[str, Any] = {
        "app_id": app_id,
        "app_secret": app_secret,
        "encrypt_key": encrypt_key,
        "verification_token": verification_token,
        "is_configured": is_configured,
    }
    if extra_config is not None:
        payload["extra_config"] = extra_config

    await _drop_im_tokens_for_config(
        stored, existing.app_id if existing else None, existing.app_secret if existing else None
    )
    await _drop_im_tokens_for_config(stored, app_id, app_secret)

    if existing:
        updated = await channel_config_dao.update(db_obj=existing, obj_in=payload)
        return updated or existing

    return await channel_config_dao.create(
        obj_in={
            "agent_id": agent_id,
            "channel_type": stored,
            **payload,
        }
    )


async def delete_channel_config(agent_id: uuid.UUID, channel_type: str) -> None:
    config = await require_channel_config(agent_id, channel_type)
    await _drop_im_tokens_for_config(config.channel_type, config.app_id, config.app_secret)
    _ = await channel_config_dao.delete(id=config.id)


async def _drop_im_tokens_for_config(
    channel_type: str,
    app_id: str | None,
    app_secret: str | None,
) -> None:
    if not app_id:
        return
    from app.services.im_token_cache import drop_cached_im_token

    secret = app_secret or ""
    kind = (normalize_channel_type(channel_type) or channel_type or "").lower()
    if kind == "feishu":
        await drop_cached_im_token("feishu", app_id, secret=secret)
    elif kind == "wecom":
        await drop_cached_im_token("wecom", app_id, secret=secret)
    elif kind == "dingtalk":
        await drop_cached_im_token("dingtalk", app_id, secret=secret)
        await drop_cached_im_token("dingtalk_oapi", app_id, secret=secret)
