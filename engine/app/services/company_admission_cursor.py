from base64 import b64decode, urlsafe_b64encode
from binascii import Error as Base64Error
from typing import ClassVar, Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.records.company_admission import AdmissionStatus


class ReviewCursor(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(strict=True, ge=1, le=1)
    tenant_id: UUID
    status: AdmissionStatus
    created_at: AwareDatetime
    id: UUID

    def encode(self) -> str:
        return urlsafe_b64encode(self.model_dump_json().encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, cursor: str) -> ReviewCursor:
        try:
            payload = b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
            return cls.model_validate_json(payload, strict=True)
        except (Base64Error, ValueError) as exc:
            raise HTTPException(400, "Invalid cursor") from exc


class ApplicantCursor(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(strict=True, ge=1, le=1)
    kind: Literal["applicant"]
    user_id: UUID
    status: AdmissionStatus | None
    created_at: AwareDatetime
    id: UUID

    def encode(self) -> str:
        return urlsafe_b64encode(self.model_dump_json().encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, cursor: str) -> ApplicantCursor:
        try:
            payload = b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
            return cls.model_validate_json(payload, strict=True)
        except (Base64Error, ValueError) as exc:
            raise HTTPException(400, "Invalid cursor") from exc
