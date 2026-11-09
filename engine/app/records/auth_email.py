"""Private durable authentication mail material and fenced claims."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import TypeAdapter


class AuthEmailKind(StrEnum):
    VERIFICATION = "verification"
    RESET = "password_reset"


@dataclass(frozen=True, slots=True)
class NewAuthEmail:
    id: UUID
    identity_id: UUID
    kind: AuthEmailKind
    expires_at: datetime
    payload: bytes


@dataclass(frozen=True, slots=True)
class AuthEmailClaim(NewAuthEmail):
    attempts: int
    claim_token: UUID
    lease_expires_at: datetime

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> AuthEmailClaim:
        return _CLAIM.validate_python(dict(row))


@dataclass(frozen=True, slots=True)
class AuthEmailMaterial:
    to_email: str
    display_name: str
    credential: str
    url: str
    password_fingerprint: str


MATERIAL: TypeAdapter[AuthEmailMaterial] = TypeAdapter(AuthEmailMaterial)
_CLAIM: TypeAdapter[AuthEmailClaim] = TypeAdapter(AuthEmailClaim)
