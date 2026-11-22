"""Converge one provider's stream clients on the configured connector rows."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from time import monotonic
from typing import Final, Protocol
from uuid import UUID

from app.core.domain.channels import ChannelType
from app.core.logging import logger
from app.dao.channel_config_dao import channel_config_dao
from app.records.channel_config import ChannelConfigRecord
from app.runtime.tasks import capped_backoff
from app.services.channels.stream_source import StreamSource, desired_stream_source

# One agent's attempts back off from one reconcile interval toward ten minutes; other agents are unaffected.
RETRY_BASE_SECONDS: Final = 30.0
RETRY_CAP_SECONDS: Final = 600.0


class StreamClients(Protocol):
    def live_source(self, agent_id: UUID, /) -> StreamSource | None:
        """Source of the running client, or None when it is missing, finished or still stopping."""
        ...

    def tracked_agents(self) -> Iterable[UUID]:
        """Agents holding any client state, including finished and still-stopping clients."""
        ...

    async def ensure_source(self, config: ChannelConfigRecord, source: StreamSource, /) -> None:
        """Under the agent's lifecycle lock, keep a live ``source`` client or replace whatever runs."""
        ...

    async def stop_client(self, agent_id: UUID, /) -> None: ...


@dataclass(slots=True)
class _Retry:
    target: StreamSource | None
    failures: int
    due_at: float


class StreamReconciler:
    """Start missing, dead or changed clients and stop revoked ones; one agent's failure never fails the pass."""

    def __init__(self, provider: ChannelType, label: str, clock: Callable[[], float] = monotonic) -> None:
        self.provider: ChannelType = provider
        self._label: str = label
        self._clock: Callable[[], float] = clock
        self._retries: dict[UUID, _Retry] = {}

    async def reconcile(self, clients: StreamClients) -> Sequence[ChannelConfigRecord]:
        """Run one pass and return the rows it read; only that configuration read can fail the pass."""
        configs = await channel_config_dao.list_configured(self.provider)
        desired: dict[UUID, tuple[ChannelConfigRecord, StreamSource]] = {}
        for config in configs:
            if (source := desired_stream_source(config)) is not None:
                desired[config.agent_id] = (config, source)
        for agent_id in set(clients.tracked_agents()) - desired.keys():
            if self._due(agent_id, None):
                await self._attempt(agent_id, None, clients.stop_client(agent_id))
        for agent_id, (config, source) in desired.items():
            if clients.live_source(agent_id) == source:
                _ = self._retries.pop(agent_id, None)
            elif self._due(agent_id, source):
                await self._attempt(agent_id, source, clients.ensure_source(config, source))
        for agent_id in self._retries.keys() - desired.keys() - set(clients.tracked_agents()):
            del self._retries[agent_id]
        return configs

    def _due(self, agent_id: UUID, target: StreamSource | None) -> bool:
        retry = self._retries.get(agent_id)
        return retry is None or retry.target != target or self._clock() >= retry.due_at

    async def _attempt(self, agent_id: UUID, target: StreamSource | None, action: Awaitable[None]) -> None:
        """Every attempt escalates until a later pass observes the wanted state, so a silent no-op also backs off."""
        retry = self._retries.get(agent_id)
        failures = retry.failures if retry is not None and retry.target == target else 0
        delay = capped_backoff(failures, base_seconds=RETRY_BASE_SECONDS, cap_seconds=RETRY_CAP_SECONDS)
        self._retries[agent_id] = _Retry(target, failures + 1, self._clock() + delay)
        try:
            await action
        except Exception as exc:
            logger.opt(exception=exc).warning(
                "{} {} for agent {} failed; next attempt in {}s",
                self._label,
                "Stop" if target is None else "Start",
                agent_id,
                delay,
            )
