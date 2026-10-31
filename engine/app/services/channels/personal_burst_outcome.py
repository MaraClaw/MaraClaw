"""Typed results of admitting one Personal WhatsApp original: accepted, a stored replay, or a handled rejection."""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from app.records.personal_burst_input import BurstOutcome, RejectedOutcome

StoredOutcome = BurstOutcome | Literal["legacy"]


@dataclass(frozen=True, slots=True)
class PersonalAccepted:
    """Charged, stored and joined to ``gateway_id``'s batch at ``ordinal``; ``started_batch`` marks a new anchor."""

    gateway_id: UUID
    ordinal: int
    started_batch: bool


@dataclass(frozen=True, slots=True)
class PersonalReplayed:
    """The native event was already decided. ``legacy`` is an accepted event from before burst batching."""

    stored: StoredOutcome


@dataclass(frozen=True, slots=True)
class PersonalRejected:
    """Terminal and durable: not queued, not charged. ``notice_created`` is False when a cooldown notice covers it."""

    outcome: RejectedOutcome
    notice_id: UUID
    notice_created: bool


PersonalAdmission = PersonalAccepted | PersonalReplayed | PersonalRejected
