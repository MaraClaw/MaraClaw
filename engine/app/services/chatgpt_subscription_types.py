"""Public browser handoff contract. Private OAuth material is never serialized here."""

from datetime import datetime
from typing import ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

ChatGPTStatus = Literal["pending", "authorized", "expired", "denied", "error"]


class ChatGPTSubscriptionStartOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    attempt_id: str
    authorization_url: str
    redirect_uri: str
    expires_in: int


class ChatGPTSubscriptionCompleteIn(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    attempt_id: str = Field(min_length=1, max_length=128)
    callback_url: str = Field(min_length=1, max_length=8192)


class ChatGPTSubscriptionStatusOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    status: ChatGPTStatus
    attempt_id: str
    model_id: UUID | None = None
    detail: str | None = None
    expires_in: int | None = None


class ChatGPTSubscriptionRefreshOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    model_id: UUID
    expires_at: datetime | None = None
