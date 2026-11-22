"""Public browser handoff contract. Private OAuth material is never serialized here."""

from datetime import datetime
from typing import ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

ChatGPTStatus = Literal["pending", "authorized", "expired", "denied", "error"]


class ChatGPTSubscriptionStartOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    attempt_id: str
    authorization_url: str
    expires_in: int


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
