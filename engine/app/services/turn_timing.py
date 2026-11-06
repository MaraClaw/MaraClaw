"""Wall-clock latency timelines for one OpenClaw turn and one report-relay attempt.

Each timeline logs exactly one structured line: ``[TurnTiming]`` per claimed gateway message and ``[RelayTiming]``
per delivery attempt. Lines carry identifiers, outcome names and integer millisecond offsets only, never prompt or
result text, tokens or secrets. Timing is diagnostics: a failure here never reaches the caller.
"""

import json
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from app.core.logging import logger
from app.records.gateway_message import GatewayMessageRecord

type TurnMark = Literal["claimed", "exec_started", "first_delta", "rpc_returned", "settled", "published"]
type TurnOutcomeKind = Literal["terminal", "not_admitted", "uncertain", "released", "failed", "cancelled"]
type RelayMark = Literal["claimed", "sent", "done"]
type RelayResult = Literal["acknowledged", "chunk_sent", "retry", "terminal", "lost"]


@dataclass(frozen=True, slots=True)
class AdapterTiming:
    """Millisecond offsets from the dispatcher process start; unreached steps stay ``None``."""

    hello_ok: int | None = None
    accepted: int | None = None
    terminal: int | None = None


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _ms(delta: timedelta) -> int:
    return delta // timedelta(milliseconds=1)


@dataclass(slots=True)
class TurnTimeline:
    """Named wall-clock marks of one claimed turn, reported as offsets from the message ``created_at``."""

    message_id: UUID
    agent_id: UUID
    created_at: datetime | None
    marks: dict[TurnMark, datetime] = field(default_factory=dict)
    adapter_timing: AdapterTiming | None = field(default=None, init=False)
    emitted: bool = field(default=False, init=False)

    @classmethod
    def claimed(cls, message: GatewayMessageRecord) -> TurnTimeline:
        """Start from a freshly claimed row, using its database claim time when present."""
        timeline = cls(message.id, message.agent_id, message.created_at)
        timeline.mark("claimed", message.delivered_at)
        return timeline

    def mark(self, name: TurnMark, at: datetime | None = None) -> None:
        self.marks[name] = at or datetime.now(UTC)

    def adapter(self, timing: AdapterTiming | None) -> None:
        """Keep dispatcher-relative durations separate: its process starts after the engine's exec mark."""
        if timing is not None:
            self.adapter_timing = timing

    def emit(self, outcome: TurnOutcomeKind) -> None:
        """Log the single timing line. Without ``created_at`` the earliest mark is the origin."""
        if self.emitted:
            return
        self.emitted = True
        with suppress(Exception):
            origin = self.created_at or min(self.marks.values(), default=None)
            fields: dict[str, str | int] = {
                "message_id": str(self.message_id),
                "agent_id": str(self.agent_id),
                "outcome": outcome,
            }
            if origin is not None:
                zero = _aware(origin)
                fields.update({name: _ms(_aware(at) - zero) for name, at in self.marks.items()})
            if self.adapter_timing is not None:
                fields.update(
                    {
                        name: value
                        for name, value in (
                            ("adapter_hello_ok", self.adapter_timing.hello_ok),
                            ("adapter_accepted", self.adapter_timing.accepted),
                            ("adapter_terminal", self.adapter_timing.terminal),
                        )
                        if value is not None
                    }
                )
            logger.info("[TurnTiming] {}", json.dumps(fields, separators=(",", ":")))


@dataclass(slots=True)
class RelayTimeline:
    """Marks of one delivery attempt, reported as offsets from the source message ``completed_at``."""

    message_id: UUID
    delivery_id: UUID
    destination_kind: str
    attempt: int
    completed_at: datetime | None = None
    result: RelayResult | None = None
    marks: dict[RelayMark, datetime] = field(default_factory=dict)

    def mark(self, name: RelayMark) -> None:
        self.marks[name] = datetime.now(UTC)

    def emit(self) -> None:
        """Log one line for a finished attempt; an attempt without a result (cancelled) logs nothing."""
        if self.result is None:
            return
        self.mark("done")
        with suppress(Exception):
            fields: dict[str, str | int] = {
                "message_id": str(self.message_id),
                "delivery_id": str(self.delivery_id),
                "destination_kind": self.destination_kind,
                "attempt": self.attempt,
                "result": self.result,
            }
            if self.completed_at is not None:
                zero = _aware(self.completed_at)
                fields.update({name: _ms(at - zero) for name, at in self.marks.items()})
            logger.info("[RelayTiming] {}", json.dumps(fields, separators=(",", ":")))
