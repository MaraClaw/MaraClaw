"""Creator-visible state, with no reusable session credentials."""

from datetime import datetime
from typing import ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.records.zalo_personal import ZaloError, ZaloRuntime, ZaloState


class ZaloPersonalQR(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    value: str = Field(max_length=768000)
    expires_at: datetime


class ZaloPersonalDiagnostics(ZaloRuntime):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class ZaloPersonalStatus(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    mode: Literal["personal_qr"] = "personal_qr"
    generation: UUID | None = None
    attempt_id: UUID | None = None
    revision: int = 0
    state: ZaloState = "disconnected"
    expires_at: datetime | None = None
    qr: ZaloPersonalQR | None = None
    account_hint: str | None = None
    error_code: ZaloError | None = None
    resumable: bool = False
    diagnostics: ZaloPersonalDiagnostics | None = None


class ZaloPersonalStart(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    expected_generation: UUID | None


class ZaloPersonalRelink(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    expected_generation: UUID
