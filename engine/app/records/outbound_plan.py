"""Versioned, credential-free provider payloads persisted before the first send."""

from typing import Literal, final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

type OutboundProvider = Literal[
    "feishu", "slack", "microsoft_teams", "google_chat", "wecom", "dingtalk", "whatsapp", "wechat", "discord"
]


@final
class OutboundTarget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: OutboundProvider
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
