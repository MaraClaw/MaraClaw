"""Typed refusals raised by the Personal burst persistence layer."""

from dataclasses import dataclass
from typing import override
from uuid import UUID


@dataclass(slots=True)
class PersonalBurstTransactionRequiredError(RuntimeError):
    """A burst mutation was attempted outside the caller-owned transaction."""

    operation: str

    @override
    def __str__(self) -> str:
        return f"Personal burst {self.operation} requires the caller's owning transaction"


@dataclass(slots=True)
class PersonalBurstParentUnavailableError(RuntimeError):
    """The shared chat session that serializes the lane is missing."""

    agent_id: UUID
    shared_session_id: UUID

    @override
    def __str__(self) -> str:
        return f"Shared session {self.shared_session_id} is unavailable for agent {self.agent_id}"


@dataclass(slots=True)
class PersonalBurstAnchorError(RuntimeError):
    """The gateway row is not a valid, current anchor/source for the requested record."""

    gateway_id: UUID

    @override
    def __str__(self) -> str:
        return f"Gateway row {self.gateway_id} is not a valid Personal burst anchor"


@dataclass(slots=True)
class PersonalBurstLimitError(RuntimeError):
    """One original exceeds the configured batch body limit; it is rejected, never truncated."""

    text_bytes: int
    batch_bytes: int

    @override
    def __str__(self) -> str:
        return f"Personal original of {self.text_bytes} bytes exceeds the {self.batch_bytes} byte batch limit"
