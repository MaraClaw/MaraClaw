"""Process-local retry pacing for native lane heads released as provably not submitted.

A released head is pending again, so the one-second recovery scan would otherwise reclaim it every tick while
its cause (stopped guest, pending bootstrap, failed preparation) persists. Each consecutive release doubles the
lane's wait, from one second up to ``MAX_SECONDS``. Any conclusive or uncertain outcome clears it. The state is
an accelerator only: losing it (restart, another instance) just restores the one-second cadence.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import final
from uuid import UUID

from app.services.openclaw_native_capacity import LaneKey

BASE_SECONDS = 1.0
MAX_SECONDS = 30.0
FORGET_AFTER_SECONDS = 300.0
PRUNE_ABOVE = 1024


@final
class ReleaseBackoff:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._entries: dict[LaneKey, tuple[int, float]] = {}

    def released(self, key: LaneKey) -> float:
        """Record one more consecutive release and return the lane's wait in seconds."""
        now = self._clock()
        previous = self._entries.get(key)
        failures = previous[0] + 1 if previous is not None and now < previous[1] + FORGET_AFTER_SECONDS else 1
        delay = min(MAX_SECONDS, BASE_SECONDS * (1 << min(failures - 1, 16)))
        self._entries[key] = (failures, now + delay)
        if len(self._entries) > PRUNE_ABOVE:
            self._prune(now)
        return delay

    def settled(self, key: LaneKey) -> None:
        _ = self._entries.pop(key, None)

    def forget_agent(self, agent_id: UUID) -> None:
        """Drop every lane of one agent, e.g. once a new guest is published for it."""
        for key in [key for key in self._entries if key.agent_id == agent_id]:
            del self._entries[key]

    def waiting(self, key: LaneKey) -> bool:
        entry = self._entries.get(key)
        return entry is not None and self._clock() < entry[1]

    def __len__(self) -> int:
        return len(self._entries)

    def _prune(self, now: float) -> None:
        for key, (_, not_before) in list(self._entries.items()):
            if now >= not_before + FORGET_AFTER_SECONDS:
                del self._entries[key]


release_backoff = ReleaseBackoff()
