"""Versioned, credential-free provider payloads persisted before the first send."""

from enum import StrEnum
from typing import Literal, final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class OutboundProvider(StrEnum):
    """Providers a persisted outbound plan can target, a subset of the stored channel types."""

    FEISHU = "feishu"
    SLACK = "slack"
    MICROSOFT_TEAMS = "microsoft_teams"
    GOOGLE_CHAT = "google_chat"
    WECOM = "wecom"
    DINGTALK = "dingtalk"
    WHATSAPP = "whatsapp"
    WECHAT = "wechat"
    DISCORD = "discord"


@final
class OutboundTarget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: OutboundProvider
    mode: Literal["cloud", "personal_qr"] = "cloud"
    config_id: UUID
    account: tuple[str, ...]
    destination: str = Field(min_length=1)
    thread: str | None = None
    recipient_type: str = ""
    endpoint: str = ""
    enterprise_agent_id: str = ""


@final
class OutboundChunk(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(min_length=1)
    key: UUID


@final
class OutboundPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1] = 1
    target: OutboundTarget
    chunks: tuple[OutboundChunk, ...] = Field(min_length=1)
