"""Resource-pressure episodes of tier-governed agents: sampling, the episode state machine and recording.

A resource is under pressure while its sampled ratio (usage over its limit) is at or above
``AGENT_RESOURCE_PRESSURE_THRESHOLD``. Each continuous stretch is one episode (``agent_resource_pressure_episodes``):
the first such sample opens it, later ones extend it, the first sample below the threshold closes it. Guests that
stop, disappear or leave the tier-governed set close their memory and CPU episodes too. An OOM-killed guest adds a
separate, idempotent row: ``agent_resource_pressure_events`` records kills live from ``docker events``, and a pass
records an ``OOMKilled`` flag only when the reader has not already stored that kill. Docker is read in one batch per
page off the event loop, and no database connection is ever held across a Docker call.
"""

from collections import defaultdict
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from anyio import to_thread

from app.config import get_settings
from app.core.logging import logger
from app.dao.agent_resource_pressure_episode_dao import OpenEpisode, agent_resource_pressure_episode_dao
from app.dao.agent_storage_dao import agent_storage_dao
from app.db.session import connection_ctx
from app.records.agent import AgentRecord
from app.records.agent_resource_pressure_episode import PressureResource
from app.records.organization_plan import OrganizationPlanId
from app.services.agent_manager import agent_manager
from app.services.agent_resource_pressure_docker import (
    DOCKER_TIMEOUT_SECONDS,
    GuestInspection,
    GuestStats,
    guest_stats,
    inspect_guests,
)
from app.services.organization_plans import AmbiguousOrganizationTierError, plan_for, tier_for_tenant

GUEST_RESOURCES: Final = (PressureResource.MEMORY, PressureResource.CPU)
_MILLICPUS_PER_CPU_PERCENT: Final = 10


@dataclass(frozen=True, slots=True)
class PressureSample:
    """One reading: ``value`` against ``limit_value`` in the resource's unit (bytes; millicpus for CPU)."""

    resource: PressureResource
    limit_value: int
    value: int
    detail: dict[str, object]

    @property
    def ratio(self) -> float:
        return self.value / self.limit_value


def _container_id(agent: AgentRecord) -> str:
    return "" if agent.container_id is None else agent.container_id


def _has_open(open_episodes: Sequence[OpenEpisode], resources: Sequence[PressureResource]) -> bool:
    return any(episode.resource in resources for episode in open_episodes)


async def _attempt(label: str, step: Awaitable[None], agent_id: UUID | None = None) -> None:
    """Await one isolated step; a failure is logged (agent id and error type only) and never stops the pass."""
    try:
        await step
    except Exception as exc:
        failure = logger.bind(exception_type=type(exc).__name__)
        if agent_id is None:
            failure.warning("[ResourcePressure] {} failed", label)
        else:
            failure.warning("[ResourcePressure] {} failed agent={}", label, agent_id)


async def observe_sample(
    agent: AgentRecord,
    tier: OrganizationPlanId,
    sample: PressureSample,
    *,
    open_episodes: Sequence[OpenEpisode] | None = None,
) -> None:
    """Apply one sample to the agent's episode of ``sample.resource``: open or extend it, or close it.

    ``open_episodes`` is the caller's prefetched view of the agent's open episodes; when given, a sample below the
    threshold skips the closing statement if none is open. It is only a hint: both statements are idempotent.
    """
    tenant_id = agent.tenant_id
    if tenant_id is None:
        return
    threshold = get_settings().AGENT_RESOURCE_PRESSURE_THRESHOLD
    known = None if open_episodes is None else [e for e in open_episodes if e.resource is sample.resource]
    if sample.ratio >= threshold:
        if known is None or any(episode.tenant_id != tenant_id for episode in known):
            _ = await agent_resource_pressure_episode_dao.close_open(
                [agent.id], [sample.resource], reason="tenant_changed", except_tenant_id=tenant_id
            )
        _ = await agent_resource_pressure_episode_dao.record_pressure(
            tenant_id=tenant_id,
            agent_id=agent.id,
            agent_name=agent.name,
            tier=tier,
            resource=sample.resource,
            limit_value=sample.limit_value,
            peak_value=sample.value,
            peak_ratio=sample.ratio,
            threshold_ratio=threshold,
            detail=sample.detail,
        )
    elif known is None or known:
        _ = await agent_resource_pressure_episode_dao.close_open(
            [agent.id], [sample.resource], reason="below_threshold"
        )


async def record_workspace_disk_pressure(
    agent: AgentRecord, tier: OrganizationPlanId, *, dir_bytes: int, layer_bytes: int, limit_bytes: int
) -> None:
    """Feed the workspace quota pass's measurement into the episodes; never raises, so it cannot break that pass."""
    sample = PressureSample(
        PressureResource.WORKSPACE_DISK,
        limit_bytes,
        dir_bytes + layer_bytes,
        {"dir_bytes": dir_bytes, "layer_bytes": layer_bytes},
    )
    await _attempt("record workspace disk", observe_sample(agent, tier, sample), agent.id)


async def close_orphaned_episodes() -> int:
    """End open episodes of agents that were deleted or are no longer tier-governed candidates."""
    return await agent_resource_pressure_episode_dao.close_orphans(reason="agent_gone")


async def _record_oom(agent: AgentRecord, tier: OrganizationPlanId, inspection: GuestInspection) -> None:
    """Store the kill behind an ``OOMKilled`` flag, unless the events reader already stored it (see its module)."""
    if agent.tenant_id is None or inspection.memory_limit <= 0:
        return
    if inspection.started_at is not None and await agent_resource_pressure_episode_dao.oom_event_recorded_since(
        agent.id, inspection.container_id, inspection.started_at
    ):
        return
    _ = await agent_resource_pressure_episode_dao.record_oom(
        tenant_id=agent.tenant_id,
        agent_id=agent.id,
        agent_name=agent.name,
        tier=tier,
        limit_value=inspection.memory_limit,
        threshold_ratio=get_settings().AGENT_RESOURCE_PRESSURE_THRESHOLD,
        occurred_at=inspection.finished_at or datetime.now(UTC),
        event_key=f"{inspection.container_id}:{inspection.finished_at_raw}",
        detail={
            "exit_code": inspection.exit_code,
            "restart_count": inspection.restart_count,
            "tier_limit": int(plan_for(tier).guest_memory_bytes),
        },
    )


def _guest_samples(tier: OrganizationPlanId, inspection: GuestInspection, reading: GuestStats) -> list[PressureSample]:
    plan = plan_for(tier)
    samples: list[PressureSample] = []
    if inspection.memory_limit > 0 and reading.memory_usage_bytes is not None:
        samples.append(
            PressureSample(
                PressureResource.MEMORY,
                inspection.memory_limit,
                reading.memory_usage_bytes,
                {"tier_limit": int(plan.guest_memory_bytes)},
            )
        )
    if inspection.millicpus_limit > 0 and reading.cpu_percent is not None:
        samples.append(
            PressureSample(
                PressureResource.CPU,
                inspection.millicpus_limit,
                round(reading.cpu_percent * _MILLICPUS_PER_CPU_PERCENT),
                {"tier_limit": int(plan.guest_millicpus)},
            )
        )
    return samples


async def _sample_guests(
    governed: Sequence[tuple[AgentRecord, OrganizationPlanId]], open_by_agent: dict[UUID, list[OpenEpisode]]
) -> None:
    """Memory, CPU and OOM readings for the page: one ``inspect`` and one ``stats`` batch."""
    with_guest = [(agent, tier, _container_id(agent)) for agent, tier in governed if _container_id(agent) != ""]
    gone = [
        agent.id
        for agent, _ in governed
        if _container_id(agent) == "" and _has_open(open_by_agent.get(agent.id, ()), GUEST_RESOURCES)
    ]
    if gone:
        _ = await agent_resource_pressure_episode_dao.close_open(gone, GUEST_RESOURCES, reason="guest_not_running")
    if not with_guest:
        return
    client = agent_manager.reacquire_docker_client()
    if client is None:
        logger.warning("[ResourcePressure] Docker unavailable; guest sampling skipped")
        return
    inspected = await to_thread.run_sync(
        inspect_guests,
        client,
        [reference for _, _, reference in with_guest],
        DOCKER_TIMEOUT_SECONDS,
        abandon_on_cancel=True,
    )
    if inspected is None:
        logger.warning("[ResourcePressure] Docker inspect failed; guest sampling skipped")
        return
    running: list[tuple[AgentRecord, OrganizationPlanId, GuestInspection]] = []
    stopped: list[UUID] = []
    for agent, tier, reference in with_guest:
        inspection = inspected.get(reference)
        if inspection is None or inspection.owner != str(agent.id):
            stopped.append(agent.id)
            continue
        if inspection.oom_killed:
            await _attempt("record OOM kill", _record_oom(agent, tier, inspection), agent.id)
        if inspection.running:
            running.append((agent, tier, inspection))
        else:
            stopped.append(agent.id)
    closing = [agent_id for agent_id in stopped if _has_open(open_by_agent.get(agent_id, ()), GUEST_RESOURCES)]
    if closing:
        _ = await agent_resource_pressure_episode_dao.close_open(closing, GUEST_RESOURCES, reason="guest_not_running")
    if not running:
        return
    readings = await to_thread.run_sync(
        guest_stats,
        client,
        [inspection.container_id for _, _, inspection in running],
        DOCKER_TIMEOUT_SECONDS,
        abandon_on_cancel=True,
    )
    if readings is None:
        logger.warning("[ResourcePressure] Docker stats failed; guest sampling skipped")
        return
    for agent, tier, inspection in running:
        reading = readings.get(inspection.container_id)
        if reading is None:
            continue
        for sample in _guest_samples(tier, inspection, reading):
            await _attempt(
                f"record {sample.resource.value} sample",
                observe_sample(agent, tier, sample, open_episodes=open_by_agent.get(agent.id, [])),
                agent.id,
            )


async def _sample_storage(
    governed: Sequence[tuple[AgentRecord, OrganizationPlanId]], open_by_agent: dict[UUID, list[OpenEpisode]]
) -> None:
    """Managed S3 usage is metered per agent (``agent_storage_usage``), so episodes carry the agent id."""
    async with connection_ctx() as db:
        charged = await agent_storage_dao.charged_for_agents(db, [agent.id for agent, _ in governed])
    for agent, tier in governed:
        used = charged.get(agent.id, 0)
        opened = open_by_agent.get(agent.id, [])
        if used == 0 and not _has_open(opened, (PressureResource.MANAGED_STORAGE,)):
            continue
        sample = PressureSample(
            PressureResource.MANAGED_STORAGE, int(plan_for(tier).storage_limit_bytes), used, {"charged_bytes": used}
        )
        await _attempt("record storage sample", observe_sample(agent, tier, sample, open_episodes=opened), agent.id)


async def sample_page(agents: Sequence[AgentRecord]) -> None:
    """Sample one page of candidate agents: guests (memory, CPU, OOM) and managed storage.

    Untiered agents lose their open episodes; an ambiguous tier link skips the agent with a warning. Every agent and
    every step is isolated, so one failure never stops the rest of the page.
    """
    tiers: dict[UUID, OrganizationPlanId | None] = {}
    ambiguous: set[UUID] = set()
    governed: list[tuple[AgentRecord, OrganizationPlanId]] = []
    untiered: list[AgentRecord] = []
    for agent in agents:
        tenant_id = agent.tenant_id
        if agent.is_system or tenant_id is None:
            untiered.append(agent)
            continue
        if tenant_id not in tiers and tenant_id not in ambiguous:
            try:
                tiers[tenant_id] = await tier_for_tenant(tenant_id)
            except AmbiguousOrganizationTierError:
                ambiguous.add(tenant_id)
        if tenant_id in ambiguous:
            logger.warning("[ResourcePressure] ambiguous organization tier; agent={} skipped", agent.id)
            continue
        tier = tiers[tenant_id]
        if tier is None:
            untiered.append(agent)
        else:
            governed.append((agent, tier))
    open_by_agent: dict[UUID, list[OpenEpisode]] = defaultdict(list)
    for episode in await agent_resource_pressure_episode_dao.open_for_agents([agent.id for agent in agents]):
        open_by_agent[episode.agent_id].append(episode)
    released = [agent.id for agent in untiered if agent.id in open_by_agent]
    if released:
        _ = await agent_resource_pressure_episode_dao.close_open(released, tuple(PressureResource), reason="untiered")
    await _attempt("sample guests", _sample_guests(governed, open_by_agent))
    await _attempt("sample storage", _sample_storage(governed, open_by_agent))
