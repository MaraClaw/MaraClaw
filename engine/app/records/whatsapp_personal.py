import re
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, Final, Literal, override
from uuid import UUID

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

PERSONAL_ROUTE_PREFIX: Final = "whatsapp_personal:"
PERSONAL_ROUTE_MAX_LENGTH: Final = 200
_COMPACT_VERSION: Final = "1"
_VERSION_FIELD: Final = re.compile(r"[1-9][0-9]*")
_UUID_HEX: Final = re.compile(r"[0-9a-f]{32}")

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


@dataclass(slots=True)
class PersonalRouteError(ValueError):
    """A personal route cannot be encoded or decoded without losing a routing fence."""

    reason: Literal["malformed", "unsupported_version", "oversize", "ambiguous_account"]

    @override
    def __str__(self) -> str:
        return "personal_route_" + self.reason


class PersonalRoute(BaseModel):
    """Routing fences carried in ``chat_sessions.external_conv_id`` (deployed ``VARCHAR(200)``).

    Compact form: ``whatsapp_personal:1:<config.hex>:<epoch.hex>:<generation.hex>:<account>:<self>``.
    Rows persisted by earlier builds hold ``whatsapp_personal:`` followed by the model's JSON; they stay readable.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    version: Literal[1] = 1
    config_id: UUID
    auth_epoch: UUID
    generation: UUID
    account_id: str
    self_jid: str

    def external_id(self) -> str:
        if ":" in self.account_id:
            raise PersonalRouteError("ambiguous_account")
        encoded = (
            f"{PERSONAL_ROUTE_PREFIX}{_COMPACT_VERSION}:{self.config_id.hex}:{self.auth_epoch.hex}:"
            f"{self.generation.hex}:{self.account_id}:{self.self_jid}"
        )
        if len(encoded) > PERSONAL_ROUTE_MAX_LENGTH:
            raise PersonalRouteError("oversize")
        return encoded

    @classmethod
    def from_external_id(cls, external: str) -> PersonalRoute:
        """Decode a compact or earlier prefixed-JSON route; never truncates, retargets or guesses."""
        if not external.startswith(PERSONAL_ROUTE_PREFIX):
            raise PersonalRouteError("malformed")
        body = external.removeprefix(PERSONAL_ROUTE_PREFIX)
        if body.startswith("{"):
            try:
                return cls.model_validate_json(body)
            except ValidationError:
                raise PersonalRouteError("malformed") from None
        version, *fields = body.split(":", 5)
        if _VERSION_FIELD.fullmatch(version) is None:
            raise PersonalRouteError("malformed")
        if version != _COMPACT_VERSION:
            raise PersonalRouteError("unsupported_version")
        if len(fields) != 5 or any(_UUID_HEX.fullmatch(field) is None for field in fields[:3]):
            raise PersonalRouteError("malformed")
        if len(external) > PERSONAL_ROUTE_MAX_LENGTH:
            raise PersonalRouteError("oversize")
        config, epoch, generation, account, self_jid = fields
        return cls(
            config_id=UUID(hex=config),
            auth_epoch=UUID(hex=epoch),
            generation=UUID(hex=generation),
            account_id=account,
            self_jid=self_jid,
        )
