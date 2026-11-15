from datetime import datetime
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

type LinkChannel = Literal["telegram", "zalo"]


class ChannelLinkStatus(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    channel: LinkChannel
    available: bool
    linked: bool
    linked_at: datetime | None
    external_hint: str | None


class ChannelLinkCodeResponse(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    channel: LinkChannel
    code: str = Field(repr=False)
    expires_at: datetime
    deep_link: str | None
    account_hint: str | None
