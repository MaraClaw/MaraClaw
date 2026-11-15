import re
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

ZaloState = Literal[
    "pending", "qr", "connected", "reconnecting", "paused", "expired", "disconnecting", "disconnected", "failed"
]
ZaloError = Literal[
    "account_in_use",
    "identity_ambiguous",
    "protocol_error",
    "login_failed",
    "auth_store_failed",
    "transport_failed",
    "qr_expired",
    "qr_declined",
    "lease_expired",
]
ProviderUID = Annotated[str, Field(pattern=r"^[0-9]{1,64}$")]
SendState = Literal["pending", "dispatch_started", "accepted", "rejected", "uncertain", "cancelled"]


@dataclass(frozen=True, slots=True)
class ZaloConnection:
    config_id: UUID
    agent_id: UUID
    tenant_id: UUID
    owner_id: UUID
    auth_epoch: UUID
    generation: UUID
    attempt_id: UUID
    revision: int
    state: ZaloState
    desired: bool
    created_at: datetime
    claim_token: UUID | None = None
    lease_until: datetime | None = None
    account_id: str | None = None
    qr_value: bytes | None = None
    qr_expires_at: datetime | None = None
    expires_at: datetime | None = None
    activated_at: datetime | None = None
    error_code: ZaloError | None = None
    purge_requested: bool = False

    @classmethod
    def from_row(cls, row: object) -> ZaloConnection:
        return TypeAdapter(cls).validate_python(row)


@dataclass(frozen=True, slots=True)
class ZaloSource:
    config_id: UUID
    auth_epoch: UUID
    generation: UUID
    claim_token: UUID
    account_id: str
    message_id: str


class ZaloRoute(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    config_id: UUID
    auth_epoch: UUID
    generation: UUID
    account_id: ProviderUID
    peer_id: ProviderUID

    def external_id(self) -> str:
        value = (
            f"zalo_personal:1:{self.config_id.hex}:{self.auth_epoch.hex}:{self.generation.hex}:"
            f"{self.account_id}:{self.peer_id}"
        )
        if len(value) > 200:
            raise ValueError("zalo_route_oversize")
        return value

    @classmethod
    def from_external_id(cls, external: str) -> ZaloRoute:
        if (
            len(external) > 200
            or re.fullmatch(r"zalo_personal:1:[0-9a-f]{32}:[0-9a-f]{32}:[0-9a-f]{32}:[0-9]{1,64}:[0-9]{1,64}", external)
            is None
        ):
            raise ValueError("zalo_route_invalid")
        _, _, config, epoch, generation, account, peer = external.split(":")
        return cls(
            config_id=UUID(config),
            auth_epoch=UUID(epoch),
            generation=UUID(generation),
            account_id=account,
            peer_id=peer,
        )


class ZaloSend(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)
    command_id: UUID
    config_id: UUID
    auth_epoch: UUID
    generation: UUID
    account_id: str
    peer_id: str
    text: str
    state: SendState
    purpose: Literal["reply", "link_notice"] = "reply"
    message_id: str | None = None
    claim_token: UUID | None = None
