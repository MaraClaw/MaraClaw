"""What one snapshot worker process remembers about each agent: its published digest, its lag and its failure backoff."""

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final
from uuid import UUID

from app.config import get_settings
from app.core.logging import logger
from app.runtime.tasks import capped_backoff
from app.services.agent_state_snapshot import list_snapshots
from app.services.storage_runtime.base import StorageBackend

STARVED_INTERVALS: Final = 3
# A failing agent is tried again at least once in this many intervals.
BACKOFF_CAP_INTERVALS: Final = 24
_monotonic = time.monotonic


class SnapshotOutcome(StrEnum):
    NO_STATE = "no_state"
    UNCHANGED = "unchanged"
    BUSY = "busy"
    INELIGIBLE = "ineligible"
    BACKING_OFF = "backing_off"
    FAILED = "failed"
    REJECTED = "rejected"
    PUBLISHED = "published"


@dataclass(frozen=True, slots=True)
class _Backoff:
    failures: int
    retry_at: float


@dataclass(slots=True)
class SnapshotTracker:
    """In process memory only: each agent's last published digest, since when it has lacked one, and its backoff."""

    digests: dict[UUID, str] = field(default_factory=dict)
    behind_since: dict[UUID, float] = field(default_factory=dict)
    backoffs: dict[UUID, _Backoff] = field(default_factory=dict)

    async def published_digest(self, store: StorageBackend, agent_id: UUID) -> str:
        """The digest of the agent's newest snapshot; a miss is seeded by one listing, an empty string when none."""
        if agent_id not in self.digests:
            newest = (await list_snapshots(store, agent_id)).newest()
            self.digests[agent_id] = "" if newest is None else newest.digest
        return self.digests[agent_id]

    def current(self, agent_id: UUID, digest: str | None = None) -> None:
        """Note an agent whose snapshot is current: its lag and its failure backoff end."""
        if digest is not None:
            self.digests[agent_id] = digest
        _ = self.behind_since.pop(agent_id, None)
        _ = self.backoffs.pop(agent_id, None)

    def behind(self, agent_id: UUID) -> None:
        """Note a pass that left the agent without a current snapshot; warn once that has lasted three intervals."""
        now = _monotonic()
        since = self.behind_since.setdefault(agent_id, now)
        if now - since > STARVED_INTERVALS * get_settings().AGENT_STATE_SNAPSHOT_INTERVAL_SECONDS:
            logger.warning("[StateSnapshot] agent={} has had no current snapshot for {:.0f}s", agent_id, now - since)

    def backing_off(self, agent_id: UUID) -> bool:
        """Whether the agent's last FAILED or REJECTED pass is still inside its backoff."""
        backoff = self.backoffs.get(agent_id)
        return backoff is not None and _monotonic() < backoff.retry_at

    def failed(self, agent_id: UUID) -> None:
        """Note a FAILED or REJECTED pass: the delay doubles with each consecutive one, from two intervals up to the cap."""
        previous = self.backoffs.get(agent_id)
        failures = 1 if previous is None else previous.failures + 1
        interval = get_settings().AGENT_STATE_SNAPSHOT_INTERVAL_SECONDS
        delay = capped_backoff(failures, base_seconds=interval, cap_seconds=BACKOFF_CAP_INTERVALS * interval)
        self.backoffs[agent_id] = _Backoff(failures, _monotonic() + delay)
