"""OpenClaw preparation and Shared-lane dispatch values (imports no service module at runtime)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final, Literal
from uuid import UUID

from app.records.agent import AgentRecord
from app.records.gateway_message import GatewayMessageRecord

if TYPE_CHECKING:
    from app.services.llm.turn import Complexity, ModelSlot

# Reserved marker on a Personal burst anchor: a missing header can never pass as a legacy, immediately ready row.
BURST_ROUTING_REASON: Final = "whatsapp_burst"

SharedBlock = Literal[
    "empty",
    "active",
    "not_due",
    "reconnecting",
    "lane_missing",
    "agent_unavailable",
    "model_unavailable",
    "config_unwritable",
    "lock_timeout",
    "cancelled",
    "lost",
]


@dataclass(frozen=True, slots=True)
class PreparedOpenClawMessage:
    """Ensured destination and routing metadata, ready for a queue-only transaction.

    A Personal burst anchor carries no selection (``selected_slot is None``) until its joined body is dispatched.
    """

    agent: AgentRecord
    content: str
    selected_slot: ModelSlot | None
    guest_model_ref: str | None
    complexity: Complexity | None
    routing_reason: str


@dataclass(frozen=True, slots=True)
class SharedDispatchRequest:
    agent_id: UUID
    now: datetime


@dataclass(frozen=True, slots=True)
class SharedClaimResult:
    """Exactly one of ``message`` (claimed and committed) or ``blocked`` (why the lane stayed as it was)."""

    message: GatewayMessageRecord | None
    blocked: SharedBlock | None = None


@dataclass(frozen=True, slots=True)
class SharedClaim:
    """Final body and routing written together with the pending -> delivered move of the actual lane head."""

    message_id: UUID
    agent_id: UUID
    conversation_id: str
    content: str
    selected_slot: str | None
    guest_model_ref: str | None
    complexity: str | None
    routing_reason: str | None
    now: datetime
