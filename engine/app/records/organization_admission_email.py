"""Durable initial-credential email intent for an approved organization admission."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import TypeAdapter

type CredentialEmailStatus = Literal["pending", "sent", "failed"]


@dataclass(frozen=True, slots=True)
class NewCredentialEmail:
    request_id: UUID
    identity_id: UUID
    to_email: str
    credential_fingerprint: str
    payload: bytes


@dataclass(frozen=True, slots=True)
class CredentialEmailState:
    """The only delivery facts that leave the service: never a payload, fingerprint or recipient secret."""

    request_id: UUID
    status: CredentialEmailStatus
    last_error: str | None
    retryable: bool

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> CredentialEmailState:
        return _STATE_ADAPTER.validate_python(dict(row))


@dataclass(frozen=True, slots=True)
class CredentialEmailClaim:
    request_id: UUID
    identity_id: UUID
    to_email: str
    credential_fingerprint: str
    payload: bytes
    message_id: str
    organization_name: str
    attempts: int
    deliver_by: datetime
    claim_token: UUID

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> CredentialEmailClaim:
        return _CLAIM_ADAPTER.validate_python(dict(row))


_STATE_ADAPTER: TypeAdapter[CredentialEmailState] = TypeAdapter(CredentialEmailState)
_CLAIM_ADAPTER: TypeAdapter[CredentialEmailClaim] = TypeAdapter(CredentialEmailClaim)
