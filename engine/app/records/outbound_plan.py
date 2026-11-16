"""Versioned, credential-free provider payloads persisted before the first send."""

from enum import StrEnum
from typing import Literal, Self, final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.records.channel_media import StoredFile


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
    TELEGRAM = "telegram"
    ZALO = "zalo_personal"


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
class OutboundFileChunk(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["file"] = "file"
    key: UUID
    file: StoredFile


@final
class OutboundPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1, 2] = 1
    target: OutboundTarget
    chunks: tuple[OutboundChunk | OutboundFileChunk, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def file_version(self) -> Self:
        if any(isinstance(chunk, OutboundFileChunk) for chunk in self.chunks) and (
            self.version != 2 or self.target.provider != OutboundProvider.ZALO or self.target.mode != "personal_qr"
        ):
            raise ValueError("File chunks require a version 2 personal Zalo plan")
        return self
