"""Personal WhatsApp burst batches: pinned scope, limits, usage and the typed handoff records."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal
from uuid import UUID

from pydantic import TypeAdapter

from app.core.json_types import datetime_from_row, int_from_row, str_from_row, uuid_from_row

BurstState = Literal["pending", "sealed", "claimed", "completed", "cancelled"]
BurstCancelReason = Literal[
    "route_stale", "disconnected", "ownership_lost", "agent_unavailable", "members_lost", "operator"
]
BurstAdmission = Literal["admit", "oversize", "capacity"]

BURST_SEPARATOR: Final = "\n\n"
BURST_CANCELLED_RESULT: Final = "whatsapp_burst_cancelled"
_STATE: Final = TypeAdapter(BurstState)
_CANCEL: Final = TypeAdapter(BurstCancelReason | None)


def required_datetime(value: object) -> datetime:
    parsed = datetime_from_row(value)
    if parsed is None:
        raise TypeError("expected a timestamp column")
    return parsed


@dataclass(frozen=True, slots=True)
class PersonalBurstScope:
    """Every fence that must be equal for originals to share one logical turn (and for a notice cooldown)."""

    agent_id: UUID
    tenant_id: UUID
    shared_session_id: UUID
    reply_session_id: UUID
    sender_user_id: UUID
    config_id: UUID
    account_id: str
    auth_epoch: UUID
    generation: UUID
    peer_id: str
    provider: str = "whatsapp"

    def params(self) -> dict[str, object]:
        return {
            "agent_id": self.agent_id,
            "tenant_id": self.tenant_id,
            "shared_session_id": self.shared_session_id,
            "reply_session_id": self.reply_session_id,
            "sender_user_id": self.sender_user_id,
            "config_id": self.config_id,
            "provider": self.provider,
            "account_id": self.account_id,
            "auth_epoch": self.auth_epoch,
            "generation": self.generation,
            "peer_id": self.peer_id,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> PersonalBurstScope:
        return cls(
            agent_id=uuid_from_row(row["agent_id"]),
            tenant_id=uuid_from_row(row["tenant_id"]),
            shared_session_id=uuid_from_row(row["shared_session_id"]),
            reply_session_id=uuid_from_row(row["reply_session_id"]),
            sender_user_id=uuid_from_row(row["sender_user_id"]),
            config_id=uuid_from_row(row["config_id"]),
            account_id=str_from_row(row["account_id"]),
            auth_epoch=uuid_from_row(row["auth_epoch"]),
            generation=uuid_from_row(row["generation"]),
            peer_id=str_from_row(row["peer_id"]),
            provider=str_from_row(row["provider"]),
        )


@dataclass(frozen=True, slots=True)
class PersonalBurstLimits:
    quiet_seconds: float
    max_seconds: float
    pending_count: int
    pending_bytes: int
    batch_bytes: int
    notice_cooldown_seconds: float

    def __post_init__(self) -> None:
        values = (
            self.quiet_seconds,
            self.max_seconds,
            self.pending_count,
            self.pending_bytes,
            self.batch_bytes,
            self.notice_cooldown_seconds,
        )
        if any(value <= 0 for value in values):
            raise ValueError("personal burst limits must be positive")
        if self.quiet_seconds > self.max_seconds:
            raise ValueError("personal burst quiet window must not exceed the maximum collection window")
        if self.batch_bytes > self.pending_bytes:
            raise ValueError("personal burst batch body limit must not exceed the pending backlog bytes")


@dataclass(frozen=True, slots=True)
class PendingUsage:
    """Waiting Personal originals (not gateway anchors), excluding the one in-flight batch."""

    count: int
    bytes: int

    def admission(self, limits: PersonalBurstLimits, incoming_bytes: int) -> BurstAdmission:
        if incoming_bytes > limits.batch_bytes:
            return "oversize"
        if self.count + 1 > limits.pending_count or self.bytes + incoming_bytes > limits.pending_bytes:
            return "capacity"
        return "admit"


@dataclass(frozen=True, slots=True)
class PersonalBurstBatch:
    gateway_id: UUID
    scope: PersonalBurstScope
    state: BurstState
    first_at: datetime
    last_at: datetime
    ready_at: datetime
    member_count: int
    member_bytes: int
    created_at: datetime
    frozen_body: str | None = None
    cancel_reason: BurstCancelReason | None = None
    sealed_at: datetime | None = None
    claimed_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def body_bytes(self) -> int:
        """Joined body size: original bytes plus the two-newline separators."""
        return self.member_bytes + len(BURST_SEPARATOR) * (self.member_count - 1)

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> PersonalBurstBatch:
        body = row["frozen_body"]
        return cls(
            gateway_id=uuid_from_row(row["gateway_id"]),
            scope=PersonalBurstScope.from_row(row),
            state=_STATE.validate_python(row["state"]),
            first_at=required_datetime(row["first_at"]),
            last_at=required_datetime(row["last_at"]),
            ready_at=required_datetime(row["ready_at"]),
            member_count=int_from_row(row["member_count"]),
            member_bytes=int_from_row(row["member_bytes"]),
            created_at=required_datetime(row["created_at"]),
            frozen_body=body if isinstance(body, str) else None,
            cancel_reason=_CANCEL.validate_python(row["cancel_reason"]),
            sealed_at=datetime_from_row(row["sealed_at"]),
            claimed_at=datetime_from_row(row["claimed_at"]),
            finished_at=datetime_from_row(row["finished_at"]),
        )


@dataclass(frozen=True, slots=True)
class PersonalBurstTail:
    """The actual last pending shared gateway row; ``batch`` is ``None`` for web/legacy/other entries."""

    gateway_id: UUID
    created_at: datetime
    batch: PersonalBurstBatch | None


@dataclass(frozen=True, slots=True)
class PersonalBurstHead:
    """The actual oldest pending shared gateway row, gated by DB-time readiness (never filtered out).

    ``due`` is True for legacy rows (no header), sealed batches and collecting batches past ``ready_at``.
    ``active`` is True while a delivered shared turn is still unreported (shared lane is single-flight).
    """

    gateway_id: UUID
    created_at: datetime
    due: bool
    active: bool
    batch: PersonalBurstBatch | None
