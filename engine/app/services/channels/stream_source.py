"""Immutable authority of a provider connection, independent of SDK shutdown."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Final
from uuid import UUID

from fastapi import HTTPException

from app.core.domain.channels import ChannelType
from app.core.json_types import json_as_str_or, json_object_from
from app.dao.channel_config_dao import channel_config_dao
from app.records.channel_config import ChannelConfigRecord

STREAM_MODES: Final = {
    ChannelType.FEISHU: "websocket",
    ChannelType.DINGTALK: "websocket",
    ChannelType.WECOM: "websocket",
    ChannelType.DISCORD: "gateway",
}


@dataclass(frozen=True, slots=True)
class StreamSource:
    config_id: UUID
    agent_id: UUID
    provider: ChannelType
    mode: str
    account_id: str | None
    credential_digest: str = field(repr=False)


def connection_mode(config: ChannelConfigRecord) -> str:
    default = "websocket" if config.channel_type == ChannelType.DINGTALK else "webhook"
    return json_as_str_or(json_object_from(config.extra_config).get("connection_mode"), default)


def _connection_credentials(config: ChannelConfigRecord) -> tuple[str, ...]:
    match config.channel_type:
        case ChannelType.FEISHU | ChannelType.DINGTALK:
            return config.app_id or "", config.app_secret or ""
        case ChannelType.WECOM:
            extra = json_object_from(config.extra_config)
            return json_as_str_or(extra.get("bot_id")), json_as_str_or(extra.get("bot_secret"))
        case ChannelType.DISCORD:
            return (config.app_secret or "",)
        case _:
            raise HTTPException(403, "Channel provider does not support this connection source")


def stream_source(config: ChannelConfigRecord) -> StreamSource:
    """Snapshot only authority, excluding mutable health and unrelated options."""
    digest = hashlib.sha256(json.dumps(_connection_credentials(config), separators=(",", ":")).encode()).hexdigest()
    return StreamSource(config.id, config.agent_id, config.channel_type, connection_mode(config), config.app_id, digest)


def check_stream_source(config: ChannelConfigRecord | None, source: StreamSource) -> ChannelConfigRecord:
    if (
        config is None
        or not config.is_configured
        or source.mode != STREAM_MODES.get(source.provider)
        or stream_source(config) != source
    ):
        raise HTTPException(403, "Connector source has been revoked")
    return config


async def authorize_stream_source(source: StreamSource) -> ChannelConfigRecord:
    """Check database authority immediately before starting an external effect."""
    return check_stream_source(await channel_config_dao.get(source.config_id), source)


async def acquire_stream_source(agent_id: UUID, provider: ChannelType, credentials: tuple[str, ...]) -> StreamSource:
    """Bind the source to the exact credentials handed to this SDK instance."""
    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type=provider)
    if config is None:
        raise HTTPException(403, "Channel configuration is unavailable")
    source = stream_source(config)
    if _connection_credentials(config) != credentials or config.channel_type != provider or config.agent_id != agent_id:
        raise HTTPException(403, "Connector credentials have been revoked")
    check_stream_source(config, source)
    return source
