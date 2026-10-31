from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, TypeAdapter

PersonalState = Literal[
    "pending", "qr", "connected", "reconnecting", "paused", "expired", "disconnecting", "disconnected", "failed"
]
PersonalError = Literal["logged_out", "account_in_use", "identity_ambiguous", "auth_store_failed", "transport_failed"]


@dataclass(frozen=True, slots=True)
class PersonalConnection:
    config_id: UUID
    agent_id: UUID
    tenant_id: UUID
    owner_id: UUID
    auth_epoch: UUID
    generation: UUID
    attempt_id: UUID
    revision: int
    state: PersonalState
    desired: bool
    created_at: datetime
    claim_token: UUID | None = None
    lease_until: datetime | None = None
    account_id: str | None = None
    pn_jid: str | None = None
    lid_jid: str | None = None
    qr_value: str | None = None
    qr_expires_at: datetime | None = None
    expires_at: datetime | None = None
    error_code: PersonalError | None = None
    purge_requested: bool = False
    activated_at: datetime | None = None

    @classmethod
    def from_row(cls, row: object) -> PersonalConnection:
        return TypeAdapter(cls).validate_python(row)


@dataclass(frozen=True, slots=True)
class PersonalSource:
    config_id: UUID
    auth_epoch: UUID
    generation: UUID
    claim_token: UUID
    account_id: str
    message_id: str


class PersonalRoute(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    version: Literal[1] = 1
    config_id: UUID
    auth_epoch: UUID
    generation: UUID
    account_id: str
    self_jid: str

    def external_id(self) -> str:
        return "whatsapp_personal:" + self.model_dump_json()
