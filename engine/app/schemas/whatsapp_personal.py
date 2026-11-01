"""Public personal QR state; no reusable authentication material."""

from datetime import datetime
from typing import ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.schemas.schemas import ChannelConfigOut


class WhatsAppChannelOut(ChannelConfigOut):
    mode: Literal["cloud", "personal_qr"] = "cloud"


type PersonalState = Literal[
    "pending", "qr", "connected", "reconnecting", "paused", "expired", "disconnecting", "disconnected", "failed"
]
type PersonalError = Literal[
    "logged_out", "account_in_use", "identity_ambiguous", "auth_store_failed", "transport_failed"
]


class PersonalQR(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    value: str
    expires_at: datetime


class PersonalStatus(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    mode: Literal["personal_qr"] = "personal_qr"
    generation: UUID | None = None
    attempt_id: UUID | None = None
    revision: int = 0
    state: PersonalState = "disconnected"
    expires_at: datetime | None = None
    qr: PersonalQR | None = None
    account_hint: str | None = None
    error_code: PersonalError | None = None


class PersonalStart(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    expected_generation: UUID | None


class PersonalRelink(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    expected_generation: UUID
