"""Resource-pressure episodes: the state machine, guest/storage sampling per page, OOM capture and the quota hook."""

from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from app.dao.agent_resource_pressure_episode_dao import OpenEpisode
from app.records.agent import AgentRecord
from app.records.agent_resource_pressure_episode import PressureResource
from app.records.organization_plan import OrganizationPlanId
from app.services import agent_resource_pressure as pressure
from app.services.agent_resource_pressure_docker import GuestInspection, GuestStats
from app.services.organization_plans import AmbiguousOrganizationTierError, plan_for

GIB = 1024**3
FINISHED = datetime(2026, 11, 13, 5, 4, 3, 123456, tzinfo=UTC)
GUEST = (PressureResource.MEMORY, PressureResource.CPU)


@dataclass
class Episode:
    tenant_id: UUID
    agent_id: UUID
    resource: PressureResource
    tier: OrganizationPlanId
    limit_value: int
    peak_value: int
    peak_ratio: float
    sample_count: int = 1
    detail: dict[str, object] = field(default_factory=dict[str, object])
    ended: bool = False
    closed_reason: str | None = None
    oom_event: str | None = None
    occurred_at: datetime | None = None


class Episodes:
    """In-memory stand-in for the episode DAO with the same open / extend / close rules as its SQL."""

    def __init__(self) -> None:
        self.rows: list[Episode] = []
        self.failing: set[UUID] = set()
        self.close_calls: list[tuple[list[UUID], tuple[PressureResource, ...], str, UUID | None]] = []
        self.open_connections: int = 0

    def open_rows(self, agent_id: UUID) -> list[Episode]:
        return [row for row in self.rows if row.agent_id == agent_id and not row.ended and row.oom_event is None]

    async def record_pressure(
        self,
        *,
        tenant_id: UUID,
        agent_id: UUID | None,
        agent_name: str,
        tier: OrganizationPlanId,
        resource: PressureResource,
        limit_value: int,
        peak_value: int,
        peak_ratio: float,
        threshold_ratio: float,
        detail: dict[str, object],
    ) -> None:
        del agent_name, threshold_ratio
        assert agent_id is not None
        if agent_id in self.failing:
            raise OSError("database down at /var/secret/path")
        for row in self.open_rows(agent_id):
            if row.resource is resource and row.tenant_id == tenant_id:
                row.sample_count += 1
                if peak_ratio > row.peak_ratio:
                    row.limit_value, row.peak_value, row.peak_ratio, row.detail = (
                        limit_value,
                        peak_value,
                        peak_ratio,
                        detail,
                    )
                return
        self.rows.append(Episode(tenant_id, agent_id, resource, tier, limit_value, peak_value, peak_ratio, 1, detail))

    async def record_oom(
        self,
        *,
        tenant_id: UUID,
        agent_id: UUID,
        agent_name: str,
        tier: OrganizationPlanId,
        limit_value: int,
        threshold_ratio: float,
        occurred_at: datetime,
        event_key: str,
        detail: dict[str, object],
    ) -> None:
        del agent_name, threshold_ratio
        if any(row.agent_id == agent_id and row.oom_event == event_key for row in self.rows):
            return
        self.rows.append(
            Episode(
                tenant_id,
                agent_id,
                PressureResource.MEMORY,
                tier,
                limit_value,
                limit_value,
                1.0,
                detail=detail,
                ended=True,
                oom_event=event_key,
                occurred_at=occurred_at,
            )
        )

    async def close_open(
        self,
        agent_ids: Sequence[UUID],
        resources: Sequence[PressureResource],
        *,
        reason: str,
        except_tenant_id: UUID | None = None,
    ) -> int:
        self.close_calls.append((list(agent_ids), tuple(resources), reason, except_tenant_id))
        closed = 0
        for agent_id in agent_ids:
            for row in self.open_rows(agent_id):
                if row.resource in resources and (except_tenant_id is None or row.tenant_id != except_tenant_id):
                    row.ended, row.closed_reason = True, reason
                    closed += 1
        return closed

    async def open_for_agents(self, agent_ids: Sequence[UUID]) -> list[OpenEpisode]:
        return [
            OpenEpisode(row.agent_id, row.tenant_id, row.resource)
            for agent_id in agent_ids
            for row in self.open_rows(agent_id)
        ]

    async def close_orphans(self, *, reason: str) -> int:
        return len(reason)


class Guests:
    """Scripted Docker answers keyed by the container each agent records."""

    def __init__(self, episodes: Episodes) -> None:
        self.episodes: Episodes = episodes
        self.inspections: dict[str, GuestInspection] = {}
        self.readings: dict[str, GuestStats] = {}
        self.inspect_calls: list[list[str]] = []
        self.stats_calls: list[list[str]] = []
        self.inspect_down: bool = False
        self.stats_down: bool = False
        self.connections_at_docker: list[int] = []

    def inspect(self, _client: object, references: list[str], _timeout: float) -> dict[str, GuestInspection] | None:
        self.inspect_calls.append(list(references))
        self.connections_at_docker.append(self.episodes.open_connections)
        if self.inspect_down:
            return None
        return {ref: self.inspections[ref] for ref in references if ref in self.inspections}

    def stats(self, _client: object, container_ids: list[str], _timeout: float) -> dict[str, GuestStats] | None:
        self.stats_calls.append(list(container_ids))
        self.connections_at_docker.append(self.episodes.open_connections)
        if self.stats_down:
            return None
        return {cid: self.readings[cid] for cid in container_ids if cid in self.readings}


class Warnings:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def bind(self, **_context: object) -> Warnings:
        return self

    def warning(self, message: str, *args: object) -> None:
        self.messages.append(message.format(*args))


@dataclass
class World:
    episodes: Episodes
    guests: Guests
    warnings: Warnings
    tiers: dict[UUID, OrganizationPlanId | Exception | None] = field(
        default_factory=dict[UUID, OrganizationPlanId | Exception | None]
    )
    charged: dict[UUID, int] = field(default_factory=dict[UUID, int])
    client_available: bool = True


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    episodes = Episodes()
    state = World(episodes, Guests(episodes), Warnings())

    async def tier_for_tenant(tenant_id: UUID | None) -> OrganizationPlanId | None:
        assert tenant_id is not None
        answer = state.tiers.get(tenant_id, OrganizationPlanId.PRO)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def charged_for_agents(_db: object, agent_ids: Sequence[UUID]) -> dict[UUID, int]:
        return {agent_id: state.charged[agent_id] for agent_id in agent_ids if agent_id in state.charged}

    @asynccontextmanager
    async def connection() -> AsyncGenerator[None]:
        episodes.open_connections += 1
        try:
            yield
        finally:
            episodes.open_connections -= 1

    monkeypatch.setattr(pressure, "agent_resource_pressure_episode_dao", episodes)
    monkeypatch.setattr(pressure, "tier_for_tenant", tier_for_tenant)
    monkeypatch.setattr(pressure, "inspect_guests", state.guests.inspect)
    monkeypatch.setattr(pressure, "guest_stats", state.guests.stats)
    monkeypatch.setattr(pressure, "connection_ctx", connection)
    monkeypatch.setattr(pressure.agent_storage_dao, "charged_for_agents", charged_for_agents)
    monkeypatch.setattr(
        pressure.agent_manager,
        "reacquire_docker_client",
        lambda: SimpleNamespace(docker_cmd=["docker"]) if state.client_available else None,
    )
    monkeypatch.setattr(pressure, "logger", state.warnings)
    return state


def _agent(tenant_id: UUID | None = None, *, container: bool = True, system: bool = False) -> AgentRecord:
    return AgentRecord(
        id=uuid4(),
        name="Ops",
        creator_id=uuid4(),
        tenant_id=uuid4() if tenant_id is None else tenant_id,
        is_system=system,
        container_id=uuid4().hex * 2 if container else None,
    )


def _guest(
    world: World,
    agent: AgentRecord,
    *,
    memory_limit: int = 1000,
    millicpus: int = 1000,
    running: bool = True,
    oom: bool = False,
    owner: UUID | None = None,
    memory_usage: int | None = 0,
    cpu_percent: float | None = 0.0,
    finished_raw: str = "2026-11-13T05:04:03.123456789Z",
) -> str:
    container_id = agent.container_id
    assert container_id is not None
    world.guests.inspections[container_id] = GuestInspection(
        container_id=container_id,
        owner=str(agent.id if owner is None else owner),
        running=running,
        memory_limit=memory_limit,
        millicpus_limit=millicpus,
        oom_killed=oom,
        finished_at=FINISHED if oom else None,
        finished_at_raw=finished_raw if oom else "",
        exit_code=137 if oom else 0,
        restart_count=3 if oom else 0,
    )
    world.guests.readings[container_id] = GuestStats(container_id, memory_usage, cpu_percent)
    return container_id


def _use(world: World, agent: AgentRecord, *, memory: int | None = None, cpu_percent: float | None = None) -> None:
    container_id = agent.container_id
    assert container_id is not None
    previous = world.guests.readings[container_id]
    world.guests.readings[container_id] = GuestStats(
        container_id,
        previous.memory_usage_bytes if memory is None else memory,
        previous.cpu_percent if cpu_percent is None else cpu_percent,
    )


def _only(world: World, resource: PressureResource) -> Episode:
    (row,) = [row for row in world.episodes.rows if row.resource is resource]
    return row


async def test_a_sample_exactly_at_the_threshold_opens_an_episode_against_the_applied_limit(world: World):
    agent = _agent()
    _ = _guest(world, agent, memory_limit=1000, memory_usage=900)

    await pressure.sample_page([agent])

    row = _only(world, PressureResource.MEMORY)
    assert (row.limit_value, row.peak_value, row.peak_ratio, row.sample_count, row.ended) == (1000, 900, 0.9, 1, False)
    assert row.tier is OrganizationPlanId.PRO
    assert row.detail == {"tier_limit": int(plan_for(OrganizationPlanId.PRO).guest_memory_bytes)}


async def test_a_sample_below_the_threshold_leaves_no_row(world: World):
    agent = _agent()
    _ = _guest(world, agent, memory_usage=899, cpu_percent=89.9)

    await pressure.sample_page([agent])

    assert world.episodes.rows == []
    assert world.episodes.close_calls == []


async def test_an_episode_extends_keeps_the_highest_peak_and_closes_below_the_threshold(world: World):
    agent = _agent()
    _ = _guest(world, agent, memory_usage=950)
    await pressure.sample_page([agent])

    _use(world, agent, memory=980)
    await pressure.sample_page([agent])
    _use(world, agent, memory=960)
    await pressure.sample_page([agent])

    row = _only(world, PressureResource.MEMORY)
    assert (row.sample_count, row.peak_value, row.peak_ratio, row.ended) == (3, 980, 0.98, False)

    _use(world, agent, memory=899)
    await pressure.sample_page([agent])

    assert (row.ended, row.closed_reason) == (True, "below_threshold")
    _use(world, agent, memory=950)
    await pressure.sample_page([agent])
    assert [r.ended for r in world.episodes.rows] == [True, False]


async def test_cpu_is_sampled_in_millicpus_against_the_applied_ceiling(world: World):
    agent = _agent()
    _ = _guest(world, agent, millicpus=2000, cpu_percent=190.0)

    await pressure.sample_page([agent])

    row = _only(world, PressureResource.CPU)
    assert (row.limit_value, row.peak_value, row.peak_ratio) == (2000, 1900, 0.95)
    assert row.detail == {"tier_limit": int(plan_for(OrganizationPlanId.PRO).guest_millicpus)}


async def test_a_resource_without_an_applied_limit_or_a_reading_is_skipped(world: World):
    unlimited, unread = _agent(), _agent()
    _ = _guest(world, unlimited, memory_limit=0, millicpus=0, memory_usage=5000, cpu_percent=500.0)
    _ = _guest(world, unread, memory_usage=None, cpu_percent=None)

    await pressure.sample_page([unlimited, unread])

    assert world.episodes.rows == []


@pytest.mark.parametrize("reason", ["stopped", "absent", "foreign", "no-container", "restarting"])
async def test_a_guest_that_is_no_longer_running_closes_its_memory_and_cpu_episodes_only(world: World, reason: str):
    agent = _agent(container=reason != "no-container")
    for resource in (*GUEST, PressureResource.MANAGED_STORAGE, PressureResource.WORKSPACE_DISK):
        world.episodes.rows.append(
            Episode(agent.tenant_id or uuid4(), agent.id, resource, OrganizationPlanId.PRO, 1000, 950, 0.95)
        )
    if reason == "stopped":
        _ = _guest(world, agent, running=False)
    elif reason == "foreign":
        _ = _guest(world, agent, owner=uuid4())
    elif reason == "restarting":
        container_id = _guest(world, agent)
        world.guests.inspections[container_id] = GuestInspection(
            container_id, str(agent.id), False, 1000, 1000, False, None, "", 0, 0
        )
    world.charged[agent.id] = int(plan_for(OrganizationPlanId.PRO).storage_limit_bytes * 0.95)
    world.guests.readings.pop(agent.container_id or "", None)

    await pressure.sample_page([agent])

    closed = {row.resource: row.closed_reason for row in world.episodes.rows if row.ended}
    assert closed == {PressureResource.MEMORY: "guest_not_running", PressureResource.CPU: "guest_not_running"}
    assert not world.guests.stats_calls


async def test_docker_being_unavailable_neither_samples_nor_closes_anything(world: World):
    agent = _agent()
    _ = _guest(world, agent, memory_usage=100)
    world.episodes.rows.append(
        Episode(agent.tenant_id or uuid4(), agent.id, PressureResource.MEMORY, OrganizationPlanId.PRO, 1000, 950, 0.95)
    )
    world.client_available = False

    await pressure.sample_page([agent])

    assert world.guests.inspect_calls == []
    assert world.episodes.close_calls == []
    assert world.warnings.messages == ["[ResourcePressure] Docker unavailable; guest sampling skipped"]


async def test_a_failed_inspect_or_stats_leaves_the_episodes_alone_but_still_records_an_oom_kill(world: World):
    agent, killed = _agent(), _agent()
    _ = _guest(world, agent, memory_usage=100)
    _ = _guest(world, killed, running=False, oom=True)
    for each in (agent, killed):
        world.episodes.rows.append(
            Episode(
                each.tenant_id or uuid4(), each.id, PressureResource.MEMORY, OrganizationPlanId.PRO, 1000, 950, 0.95
            )
        )

    world.guests.inspect_down = True
    await pressure.sample_page([agent, killed])
    assert [row.ended for row in world.episodes.rows] == [False, False]
    assert world.warnings.messages == ["[ResourcePressure] Docker inspect failed; guest sampling skipped"]

    world.guests.inspect_down, world.guests.stats_down = False, True
    await pressure.sample_page([agent, killed])

    assert [row.ended for row in world.episodes.rows[:2]] == [False, True]
    assert len([row for row in world.episodes.rows if row.oom_event is not None]) == 1
    assert world.warnings.messages[-1] == "[ResourcePressure] Docker stats failed; guest sampling skipped"


async def test_a_page_costs_one_inspect_and_one_stats_batch_and_stats_covers_only_running_guests(world: World):
    running = [_agent() for _ in range(3)]
    stopped = _agent()
    ids = [_guest(world, agent) for agent in running]
    stopped_id = _guest(world, stopped, running=False)

    await pressure.sample_page([*running, stopped])

    assert world.guests.inspect_calls == [[*ids, stopped_id]]
    assert world.guests.stats_calls == [ids]


async def test_no_database_connection_is_held_while_docker_runs(world: World):
    agent = _agent()
    _ = _guest(world, agent, memory_usage=950)
    world.charged[agent.id] = 100

    await pressure.sample_page([agent])

    assert world.guests.connections_at_docker == [0, 0]
    assert world.episodes.open_connections == 0


async def test_an_oom_kill_is_recorded_once_against_the_applied_limit(world: World):
    agent = _agent()
    _ = _guest(world, agent, memory_limit=3 * GIB, running=False, oom=True)

    await pressure.sample_page([agent])
    await pressure.sample_page([agent])

    (row,) = world.episodes.rows
    assert row.oom_event == f"{agent.container_id}:2026-11-13T05:04:03.123456789Z"
    assert (row.resource, row.limit_value, row.peak_ratio, row.occurred_at) == (
        PressureResource.MEMORY,
        3 * GIB,
        1.0,
        FINISHED,
    )
    assert row.detail == {
        "exit_code": 137,
        "restart_count": 3,
        "tier_limit": int(plan_for(OrganizationPlanId.PRO).guest_memory_bytes),
    }


async def test_a_later_oom_kill_of_the_same_guest_is_a_new_row_and_one_without_a_limit_is_not_recorded(world: World):
    agent, unlimited = _agent(), _agent()
    _ = _guest(world, agent, running=False, oom=True)
    _ = _guest(world, unlimited, memory_limit=0, running=False, oom=True)
    await pressure.sample_page([agent, unlimited])

    _ = _guest(world, agent, running=False, oom=True, finished_raw="2026-11-14T01:00:00Z")
    await pressure.sample_page([agent, unlimited])

    assert sorted(row.oom_event or "" for row in world.episodes.rows) == [
        f"{agent.container_id}:2026-11-13T05:04:03.123456789Z",
        f"{agent.container_id}:2026-11-14T01:00:00Z",
    ]


async def test_agents_without_a_tier_lose_their_open_episodes_and_are_never_sampled(world: World):
    untiered, system = _agent(), _agent(system=True)
    world.tiers[untiered.tenant_id or uuid4()] = None
    for agent in (untiered, system):
        _ = _guest(world, agent, memory_usage=990)
        world.charged[agent.id] = 990
        for resource in PressureResource:
            world.episodes.rows.append(
                Episode(agent.tenant_id or uuid4(), agent.id, resource, OrganizationPlanId.PRO, 1000, 950, 0.95)
            )

    await pressure.sample_page([untiered, system])

    assert {row.closed_reason for row in world.episodes.rows} == {"untiered"}
    assert len(world.episodes.rows) == 8
    assert world.guests.inspect_calls == []


async def test_an_ambiguous_tier_skips_the_agent_with_a_warning_naming_only_its_id(world: World):
    agent, other = _agent(), _agent()
    world.tiers[agent.tenant_id or uuid4()] = AmbiguousOrganizationTierError(agent.tenant_id or uuid4())
    world.episodes.rows.append(
        Episode(agent.tenant_id or uuid4(), agent.id, PressureResource.MEMORY, OrganizationPlanId.PRO, 1000, 950, 0.95)
    )
    _ = _guest(world, agent, memory_usage=100)
    _ = _guest(world, other, memory_usage=950)

    await pressure.sample_page([agent, other])

    assert world.warnings.messages == [f"[ResourcePressure] ambiguous organization tier; agent={agent.id} skipped"]
    assert [row.ended for row in world.episodes.rows if row.agent_id == agent.id] == [False]
    assert world.guests.inspect_calls == [[other.container_id]]
    assert _only_for(world, other.id, PressureResource.MEMORY).peak_value == 950


def _only_for(world: World, agent_id: UUID, resource: PressureResource) -> Episode:
    (row,) = [row for row in world.episodes.rows if row.agent_id == agent_id and row.resource is resource]
    return row


async def test_a_tier_is_resolved_once_per_organization_on_a_page(world: World, monkeypatch: pytest.MonkeyPatch):
    tenant_id = uuid4()
    resolve = AsyncMock(return_value=OrganizationPlanId.MAX)
    monkeypatch.setattr(pressure, "tier_for_tenant", resolve)
    agents = [_agent(tenant_id, container=False) for _ in range(3)]

    await pressure.sample_page(agents)

    resolve.assert_awaited_once_with(tenant_id)


async def test_managed_storage_is_sampled_per_agent_against_the_tier_allowance(world: World):
    heavy, light, idle = _agent(container=False), _agent(container=False), _agent(container=False)
    limit = int(plan_for(OrganizationPlanId.PRO).storage_limit_bytes)
    world.charged[heavy.id] = int(limit * 0.95)
    world.charged[light.id] = int(limit * 0.5)

    await pressure.sample_page([heavy, light, idle])

    row = _only(world, PressureResource.MANAGED_STORAGE)
    assert (row.agent_id, row.limit_value, row.peak_value) == (heavy.id, limit, int(limit * 0.95))
    assert world.episodes.close_calls == []

    world.charged[heavy.id] = int(limit * 0.5)
    await pressure.sample_page([heavy, light, idle])

    assert (row.ended, row.closed_reason) == (True, "below_threshold")
    assert [call[0] for call in world.episodes.close_calls] == [[heavy.id]]


async def test_storage_that_was_wiped_closes_an_open_episode_even_without_a_usage_row(world: World):
    agent = _agent(container=False)
    world.episodes.rows.append(
        Episode(
            agent.tenant_id or uuid4(), agent.id, PressureResource.MANAGED_STORAGE, OrganizationPlanId.PRO, 10, 9, 0.9
        )
    )

    await pressure.sample_page([agent])

    assert world.episodes.rows[0].closed_reason == "below_threshold"


async def test_moving_to_another_organization_ends_the_old_episode_and_opens_one_there(world: World):
    agent = _agent()
    old_tenant = uuid4()
    world.episodes.rows.append(
        Episode(old_tenant, agent.id, PressureResource.MEMORY, OrganizationPlanId.MAX, 1000, 950, 0.95)
    )
    _ = _guest(world, agent, memory_usage=960)

    await pressure.sample_page([agent])

    old, new = world.episodes.rows
    assert (old.ended, old.closed_reason) == (True, "tenant_changed")
    assert (new.tenant_id, new.ended) == (agent.tenant_id, False)
    assert world.episodes.close_calls[0][3] == agent.tenant_id


async def test_one_agents_failure_is_logged_without_details_and_the_rest_of_the_page_is_recorded(world: World):
    broken, fine = _agent(), _agent()
    _ = _guest(world, broken, memory_usage=950)
    _ = _guest(world, fine, memory_usage=950)
    world.episodes.failing.add(broken.id)

    await pressure.sample_page([broken, fine])

    assert [row.agent_id for row in world.episodes.rows] == [fine.id]
    assert world.warnings.messages == [f"[ResourcePressure] record memory sample failed agent={broken.id}"]
    assert all("secret" not in message for message in world.warnings.messages)


async def test_workspace_disk_pressure_opens_extends_and_closes_with_the_measurement_detail(world: World):
    agent = _agent()
    record = pressure.record_workspace_disk_pressure

    await record(agent, OrganizationPlanId.PRO, dir_bytes=900, layer_bytes=100, limit_bytes=1000)
    await record(agent, OrganizationPlanId.PRO, dir_bytes=950, layer_bytes=50, limit_bytes=1000)

    row = _only(world, PressureResource.WORKSPACE_DISK)
    assert (row.limit_value, row.peak_value, row.sample_count, row.ended) == (1000, 1000, 2, False)
    assert row.detail == {"dir_bytes": 900, "layer_bytes": 100}

    await record(agent, OrganizationPlanId.PRO, dir_bytes=400, layer_bytes=100, limit_bytes=1000)

    assert (row.ended, row.closed_reason) == (True, "below_threshold")


async def test_workspace_disk_pressure_after_a_transfer_ends_the_old_organizations_episode(world: World):
    agent = _agent()
    old_tenant = uuid4()
    world.episodes.rows.append(
        Episode(old_tenant, agent.id, PressureResource.WORKSPACE_DISK, OrganizationPlanId.MAX, 1000, 950, 0.95)
    )

    await pressure.record_workspace_disk_pressure(
        agent, OrganizationPlanId.PRO, dir_bytes=950, layer_bytes=0, limit_bytes=1000
    )

    old, new = world.episodes.rows
    assert (old.ended, old.closed_reason) == (True, "tenant_changed")
    assert (new.tenant_id, new.resource, new.ended) == (agent.tenant_id, PressureResource.WORKSPACE_DISK, False)


async def test_workspace_disk_pressure_never_raises(world: World):
    agent = _agent()
    world.episodes.failing.add(agent.id)

    await pressure.record_workspace_disk_pressure(
        agent, OrganizationPlanId.PRO, dir_bytes=990, layer_bytes=0, limit_bytes=1000
    )

    assert world.warnings.messages == [f"[ResourcePressure] record workspace disk failed agent={agent.id}"]


async def test_a_tenantless_agent_is_never_recorded(world: World):
    agent = AgentRecord(id=uuid4(), name="x", creator_id=uuid4(), tenant_id=None)

    await pressure.observe_sample(
        agent,
        OrganizationPlanId.PRO,
        pressure.PressureSample(PressureResource.WORKSPACE_DISK, 1000, 990, {}),
    )

    assert world.episodes.rows == []


async def test_orphaned_episodes_are_closed_with_the_agent_gone_reason(world: World, monkeypatch: pytest.MonkeyPatch):
    close = AsyncMock(return_value=2)
    monkeypatch.setattr(world.episodes, "close_orphans", close)

    assert await pressure.close_orphaned_episodes() == 2

    close.assert_awaited_once_with(reason="agent_gone")
