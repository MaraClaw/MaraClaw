"""Resource-pressure episodes against live PostgreSQL (``MARACLAW_TEST_POSTGRES_DSN``): the SQL contract of the table."""

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from agent_deletion_fixtures import DeletionScenario, deletion_scenario
from app.dao.agent_dao import agent_dao
from app.dao.agent_resource_pressure_episode_dao import (
    EpisodeFilters,
    EpisodePosition,
    agent_resource_pressure_episode_dao as dao,
)
from app.dao.tenant_dao import tenant_dao
from app.db.errors import CheckViolationError, UniqueViolationError
from app.records.agent_resource_pressure_episode import (
    AgentResourcePressureEpisodeRecord,
    PressureKind,
    PressureResource,
)
from app.records.organization_plan import OrganizationPlanId
from app.services import agent_resource_pressure as pressure, agent_resource_pressure_report as report
from app.services.agent_resource_pressure_docker import GuestInspection, GuestStats
from tier_limit_fixtures import approve_admission_for

__all__ = ["deletion_scenario"]

GIB = 1024**3
PRO, MAX = OrganizationPlanId.PRO, OrganizationPlanId.MAX
MEMORY, CPU = PressureResource.MEMORY, PressureResource.CPU
DISK, STORAGE = PressureResource.WORKSPACE_DISK, PressureResource.MANAGED_STORAGE
T0 = datetime(2026, 11, 1, 8, 0, tzinfo=UTC)
BASELINE = Path(__file__).resolve().parents[1] / "scripts" / "schema_baseline.sql"
INSERT = (
    "INSERT INTO agent_resource_pressure_episodes (id, tenant_id, agent_id, agent_name, tier, resource, kind, "
    "limit_value, peak_value, peak_ratio, threshold_ratio, started_at, last_seen_at, ended_at, sample_count, "
    "event_key) VALUES (%(id)s, %(tenant_id)s, %(agent_id)s, %(agent_name)s, %(tier)s, %(resource)s, %(kind)s, "
    "%(limit_value)s, %(peak_value)s, %(peak_ratio)s, %(threshold_ratio)s, %(started_at)s, %(last_seen_at)s, "
    "%(ended_at)s, %(sample_count)s, %(event_key)s)"
)


async def _insert(
    scenario: DeletionScenario, tenant_id: UUID, agent_id: UUID | None, **overrides: object
) -> dict[str, object]:
    values: dict[str, object] = {
        "id": uuid4(),
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "agent_name": "Ops",
        "tier": "pro",
        "resource": "memory",
        "kind": "threshold",
        "limit_value": 1000,
        "peak_value": 950,
        "peak_ratio": 0.95,
        "threshold_ratio": 0.9,
        "started_at": T0,
        "last_seen_at": T0,
        "ended_at": None,
        "sample_count": 1,
        "event_key": None,
    }
    values.update(overrides)
    await scenario.db.execute(INSERT, values)
    return values


async def _episodes(scenario: DeletionScenario) -> list[dict[str, object]]:
    rows = await scenario.rows("SELECT * FROM agent_resource_pressure_episodes ORDER BY started_at, id")
    return [dict(row) for row in rows]


async def _open(
    tenant_id: UUID,
    agent_id: UUID | None,
    *,
    resource: PressureResource = MEMORY,
    value: int = 950,
    limit: int = 1000,
    tier: OrganizationPlanId = PRO,
    name: str = "Ops",
    detail: dict[str, object] | None = None,
) -> AgentResourcePressureEpisodeRecord:
    return await dao.record_pressure(
        tenant_id=tenant_id,
        agent_id=agent_id,
        agent_name=name,
        tier=tier,
        resource=resource,
        limit_value=limit,
        peak_value=value,
        peak_ratio=value / limit,
        threshold_ratio=0.9,
        detail=detail or {},
    )


async def test_one_threshold_episode_is_open_per_agent_and_resource_until_it_closes(
    deletion_scenario: DeletionScenario,
):
    scenario = deletion_scenario
    agent, other = await scenario.agent("owner"), await scenario.agent("owner", "Other")
    _ = await _insert(scenario, scenario.tenant_id, agent)

    with pytest.raises(UniqueViolationError):
        _ = await _insert(scenario, scenario.tenant_id, agent)
    _ = await _insert(scenario, scenario.tenant_id, agent, resource="cpu")
    _ = await _insert(scenario, scenario.tenant_id, other)
    _ = await _insert(scenario, scenario.tenant_id, agent, started_at=T0 - timedelta(days=1), ended_at=T0)
    await scenario.db.execute("UPDATE agent_resource_pressure_episodes SET ended_at = %(at)s", {"at": T0})
    _ = await _insert(scenario, scenario.tenant_id, agent)

    assert len(await _episodes(scenario)) == 5


async def test_an_open_episode_without_an_agent_is_still_one_per_organization_and_resource(
    deletion_scenario: DeletionScenario,
):
    scenario = deletion_scenario
    _ = await _insert(scenario, scenario.tenant_id, None, resource="managed_storage")

    with pytest.raises(UniqueViolationError):
        _ = await _insert(scenario, scenario.tenant_id, None, resource="managed_storage")
    _ = await _insert(scenario, scenario.foreign_tenant_id, None, resource="managed_storage")
    _ = await _insert(scenario, scenario.tenant_id, None, resource="memory")


async def test_an_oom_kill_is_keyed_by_agent_and_event_and_never_competes_for_the_open_slot(
    deletion_scenario: DeletionScenario,
):
    scenario = deletion_scenario
    agent, other = await scenario.agent("owner"), await scenario.agent("owner", "Other")
    kill = {"kind": "oom_kill", "ended_at": T0, "event_key": "c1:t1", "peak_ratio": 1.0, "peak_value": 1000}
    _ = await _insert(scenario, scenario.tenant_id, agent, **kill)

    with pytest.raises(UniqueViolationError):
        _ = await _insert(scenario, scenario.tenant_id, agent, **kill)
    _ = await _insert(scenario, scenario.tenant_id, agent, **{**kill, "event_key": "c1:t2"})
    _ = await _insert(scenario, scenario.tenant_id, other, **kill)
    _ = await _insert(scenario, scenario.tenant_id, agent)


async def test_oom_recording_is_idempotent_per_event_and_stores_the_kill_as_a_closed_row(
    deletion_scenario: DeletionScenario,
):
    scenario = deletion_scenario
    agent = await scenario.agent("owner")

    async def kill(event_key: str, at: datetime):
        return await dao.record_oom(
            tenant_id=scenario.tenant_id,
            agent_id=agent,
            agent_name="Ops",
            tier=PRO,
            limit_value=2 * GIB,
            threshold_ratio=0.9,
            occurred_at=at,
            event_key=event_key,
            detail={"exit_code": 137, "restart_count": 1},
        )

    first = await kill("c1:t1", T0)
    again = await kill("c1:t1", T0 + timedelta(hours=1))
    later = await kill("c1:t2", T0 + timedelta(hours=2))

    assert first is not None
    assert again is None
    assert later is not None
    assert (first.kind, first.resource, first.limit_value, first.peak_value, first.peak_ratio) == (
        PressureKind.OOM_KILL,
        MEMORY,
        2 * GIB,
        2 * GIB,
        1.0,
    )
    assert (first.started_at, first.last_seen_at, first.ended_at, first.sample_count) == (T0, T0, T0, 1)
    assert first.event_key == "c1:t1"
    assert first.detail == {"exit_code": 137, "restart_count": 1}
    assert len(await _episodes(scenario)) == 2


async def test_the_pass_correlation_query_matches_only_reader_rows_of_that_agents_container_since_the_start(
    deletion_scenario: DeletionScenario,
):
    scenario = deletion_scenario
    agent, other = await scenario.agent("owner"), await scenario.agent("owner", "Other")
    container, sibling = "ab" * 32, "abab"
    kill = {"kind": "oom_kill", "peak_ratio": 1.0, "peak_value": 1000}

    async def stored(agent_id: UUID, event_key: str, at: datetime) -> None:
        _ = await _insert(
            scenario,
            scenario.tenant_id,
            agent_id,
            **kill,
            event_key=event_key,
            started_at=at,
            last_seen_at=at,
            ended_at=at,
        )

    await stored(agent, f"{container}:1794592962998812135", T0)
    await stored(agent, f"{container}:2026-11-13T18:02:43.291703135Z", T0 + timedelta(hours=1))
    await stored(agent, f"{sibling}:1794592962998812135", T0 + timedelta(hours=2))
    await stored(other, f"{container}:1794592962998812135", T0 + timedelta(hours=3))

    assert await dao.oom_event_recorded_since(agent, container, T0) is True
    assert await dao.oom_event_recorded_since(agent, container, T0 + timedelta(microseconds=1)) is False
    assert await dao.oom_event_recorded_since(agent, sibling, T0) is True
    assert await dao.oom_event_recorded_since(agent, sibling[:2], T0) is False
    assert await dao.oom_event_recorded_since(other, container, T0 + timedelta(hours=3)) is True
    assert await dao.oom_event_recorded_since(other, sibling, T0) is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"resource": "network"},
        {"kind": "burst"},
        {"tier": "free"},
        {"limit_value": 0},
        {"peak_value": -1},
        {"peak_ratio": -0.1},
        {"threshold_ratio": 0},
        {"threshold_ratio": 1.1},
        {"sample_count": 0},
        {"last_seen_at": T0 - timedelta(seconds=1)},
        {"ended_at": T0 - timedelta(seconds=1)},
        {"kind": "oom_kill", "ended_at": T0},
        {"kind": "oom_kill", "event_key": "k"},
        {"kind": "oom_kill", "event_key": "k", "ended_at": T0, "resource": "cpu"},
        {"event_key": "k"},
    ],
    ids=[
        "resource",
        "kind",
        "tier",
        "zero-limit",
        "negative-peak",
        "negative-ratio",
        "zero-threshold",
        "threshold-above-one",
        "no-samples",
        "seen-before-start",
        "ended-before-start",
        "oom-without-key",
        "oom-without-end",
        "oom-not-memory",
        "threshold-with-key",
    ],
)
async def test_rows_keep_their_units_kinds_and_timestamps_sane(
    deletion_scenario: DeletionScenario, overrides: dict[str, object]
):
    scenario = deletion_scenario

    with pytest.raises(CheckViolationError):
        _ = await _insert(scenario, scenario.tenant_id, await scenario.agent("owner"), **overrides)


async def test_concurrent_samples_of_one_resource_converge_on_one_open_episode(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    agent = await scenario.agent("owner")

    await asyncio.gather(
        *(_open(scenario.tenant_id, agent, value=900 + number) for number in range(16)),
    )

    (row,) = await _episodes(scenario)
    assert (row["sample_count"], row["peak_value"], row["ended_at"]) == (16, 915, None)


async def test_an_episode_keeps_its_highest_peak_with_its_limit_and_detail_and_its_opening_tier(
    deletion_scenario: DeletionScenario,
):
    scenario = deletion_scenario
    agent = await scenario.agent("owner")

    opened = await _open(scenario.tenant_id, agent, value=920, detail={"sample": 1})
    higher = await _open(
        scenario.tenant_id, agent, value=2940, limit=3000, tier=MAX, name="Renamed", detail={"sample": 2}
    )
    lower = await _open(scenario.tenant_id, agent, value=950, limit=1000, name="Renamed", detail={"sample": 3})

    assert opened.id == higher.id == lower.id
    assert (opened.sample_count, higher.sample_count, lower.sample_count) == (1, 2, 3)
    assert (lower.limit_value, lower.peak_value, lower.peak_ratio, lower.detail) == (3000, 2940, 0.98, {"sample": 2})
    assert (lower.tier, lower.threshold_ratio, lower.agent_name, lower.ended_at) == (PRO, 0.9, "Renamed", None)
    assert opened.started_at == opened.last_seen_at
    assert lower.last_seen_at >= higher.last_seen_at >= opened.started_at


async def test_closing_ends_only_the_named_resources_of_the_named_agents_and_notes_the_reason(
    deletion_scenario: DeletionScenario,
):
    scenario = deletion_scenario
    agent, other = await scenario.agent("owner"), await scenario.agent("owner", "Other")
    for resource in (MEMORY, CPU, DISK):
        _ = await _open(scenario.tenant_id, agent, resource=resource)
    _ = await _open(scenario.tenant_id, other)

    closed = await dao.close_open([agent], (MEMORY, CPU), reason="guest_not_running")
    again = await dao.close_open([agent], (MEMORY, CPU), reason="guest_not_running")

    assert (closed, again) == (2, 0)
    rows = {(row["agent_id"], row["resource"]): row for row in await _episodes(scenario)}
    assert [rows[(agent, resource)]["ended_at"] is not None for resource in ("memory", "cpu", "workspace_disk")] == [
        True,
        True,
        False,
    ]
    assert rows[(other, "memory")]["ended_at"] is None
    inverted = await scenario.rows(
        "SELECT count(*) AS n FROM agent_resource_pressure_episodes WHERE ended_at < last_seen_at"
    )
    assert inverted[0]["n"] == 0
    reasons = await scenario.rows(
        "SELECT resource, detail->>'closed_reason' AS reason FROM agent_resource_pressure_episodes "
        "WHERE ended_at IS NOT NULL ORDER BY resource"
    )
    assert [(row["resource"], row["reason"]) for row in reasons] == [
        ("cpu", "guest_not_running"),
        ("memory", "guest_not_running"),
    ]
    reopened = await _open(scenario.tenant_id, agent)
    assert reopened.sample_count == 1
    assert len(await _episodes(scenario)) == 5


async def test_closing_with_an_excepted_organization_ends_only_the_episodes_of_other_organizations(
    deletion_scenario: DeletionScenario,
):
    scenario = deletion_scenario
    agent = await scenario.agent("owner")
    _ = await _open(scenario.foreign_tenant_id, agent)
    _ = await _open(scenario.tenant_id, agent, resource=CPU)

    closed = await dao.close_open([agent], (MEMORY, CPU), reason="tenant_changed", except_tenant_id=scenario.tenant_id)

    assert closed == 1
    ended = {row["resource"]: row["ended_at"] is not None for row in await _episodes(scenario)}
    assert ended == {"memory": True, "cpu": False}


async def test_episodes_survive_their_agent_and_orphan_sweeps_end_only_the_open_ones_of_missing_agents(
    deletion_scenario: DeletionScenario,
):
    scenario = deletion_scenario
    gone = await scenario.agent("owner", "Gone")
    system = await scenario.agent("owner", "System", is_system=True)
    stranded = await scenario.agent("owner", "Stranded")
    healthy = await scenario.agent("owner", "Healthy")
    await scenario.db.execute("UPDATE agents SET tenant_id = NULL WHERE id = %(id)s", {"id": stranded})
    for agent in (gone, system, stranded, healthy):
        _ = await _open(scenario.tenant_id, agent, name=str(agent))
    _ = await _insert(
        scenario,
        scenario.tenant_id,
        gone,
        resource="cpu",
        started_at=T0 - timedelta(days=1),
        last_seen_at=T0 - timedelta(days=1),
        ended_at=T0 - timedelta(hours=20),
    )
    _ = await dao.record_oom(
        tenant_id=scenario.tenant_id,
        agent_id=gone,
        agent_name="Gone",
        tier=PRO,
        limit_value=1000,
        threshold_ratio=0.9,
        occurred_at=T0,
        event_key="c:t",
        detail={},
    )

    await agent_dao.delete_with_related(gone)

    assert len(await _episodes(scenario)) == 6
    swept = await dao.close_orphans(reason="agent_gone")
    assert swept == 3
    rows = await _episodes(scenario)
    open_agents = {row["agent_id"] for row in rows if row["ended_at"] is None}
    assert open_agents == {healthy}
    closed = await scenario.rows(
        "SELECT agent_id, detail FROM agent_resource_pressure_episodes WHERE detail ? 'closed_reason'"
    )
    assert {row["agent_id"] for row in closed} == {gone, system, stranded}
    assert await dao.close_orphans(reason="agent_gone") == 0
    assert len(rows) == 6


async def test_deleting_a_company_removes_its_episodes_and_only_its_own(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    mine, abroad = (
        await scenario.agent("owner"),
        await scenario.agent("owner", "Abroad", tenant_id=scenario.foreign_tenant_id),
    )
    _ = await _open(scenario.tenant_id, mine)
    _ = await _open(scenario.foreign_tenant_id, abroad)
    _ = await _open(scenario.foreign_tenant_id, None, resource=STORAGE)
    _ = await dao.record_oom(
        tenant_id=scenario.foreign_tenant_id,
        agent_id=abroad,
        agent_name="Abroad",
        tier=PRO,
        limit_value=1000,
        threshold_ratio=0.9,
        occurred_at=T0,
        event_key="c:t",
        detail={},
    )

    await tenant_dao.delete_cascade(scenario.foreign_tenant_id)

    rows = await _episodes(scenario)
    assert [row["tenant_id"] for row in rows] == [scenario.tenant_id]


async def _seed_report(scenario: DeletionScenario) -> dict[str, UUID]:
    one, two = await scenario.agent("owner", "One"), await scenario.agent("owner", "Two")
    tenant, abroad = scenario.tenant_id, scenario.foreign_tenant_id
    _ = await _insert(
        scenario, tenant, one, started_at=T0, last_seen_at=T0 + timedelta(minutes=10), ended_at=T0 + timedelta(minutes=15),
        peak_value=int(1.9 * GIB), peak_ratio=0.95,
    )  # fmt: skip
    _ = await _insert(
        scenario, tenant, two, started_at=T0 + timedelta(hours=1), last_seen_at=T0 + timedelta(hours=1, minutes=30),
        peak_value=int(1.96 * GIB), peak_ratio=0.98,
    )  # fmt: skip
    _ = await _insert(
        scenario, tenant, one, resource="cpu", started_at=T0 + timedelta(hours=2), last_seen_at=T0 + timedelta(hours=2),
        ended_at=T0 + timedelta(hours=2, minutes=5), peak_value=950,
    )  # fmt: skip
    _ = await _insert(
        scenario, tenant, one, kind="oom_kill", event_key="c:t", started_at=T0 + timedelta(hours=3),
        last_seen_at=T0 + timedelta(hours=3), ended_at=T0 + timedelta(hours=3), peak_ratio=1.0, peak_value=2 * GIB,
    )  # fmt: skip
    _ = await _insert(
        scenario, tenant, one, resource="workspace_disk", tier="max", started_at=T0 + timedelta(hours=4),
        last_seen_at=T0 + timedelta(hours=4, minutes=20), ended_at=T0 + timedelta(hours=4, minutes=25),
        peak_value=int(4.6 * GIB), peak_ratio=0.92,
    )  # fmt: skip
    _ = await _insert(
        scenario, abroad, None, resource="managed_storage", started_at=T0 + timedelta(hours=5),
        last_seen_at=T0 + timedelta(hours=5, minutes=40), peak_value=int(0.97 * GIB), peak_ratio=0.97,
    )  # fmt: skip
    return {"one": one, "two": two}


async def test_aggregates_count_per_resource_and_organization_most_recent_first(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    _ = await _seed_report(scenario)

    rows = await dao.tenant_aggregates(EpisodeFilters())

    assert [row.tenant_name for row in rows] == ["Other", "Acme"]
    abroad, home = rows
    assert abroad.episode_counts == {MEMORY: 0, CPU: 0, DISK: 0, STORAGE: 1}
    assert (abroad.oom_count, abroad.pressure_seconds, abroad.agents_affected, abroad.max_peak_ratio) == (
        0,
        2400,
        0,
        0.97,
    )
    assert home.episode_counts == {MEMORY: 2, CPU: 1, DISK: 1, STORAGE: 0}
    assert (home.oom_count, home.pressure_seconds, home.agents_affected, home.max_peak_ratio) == (
        1,
        600 + 1800 + 0 + 1200,
        2,
        1.0,
    )
    assert home.last_seen_at == T0 + timedelta(hours=4, minutes=20)


@pytest.mark.parametrize(
    ("filters", "expected_resources"),
    [
        (EpisodeFilters(since=T0 + timedelta(hours=1), until=T0 + timedelta(hours=3)), ["memory", "cpu"]),
        (EpisodeFilters(until=T0 + timedelta(hours=1)), ["memory"]),
        (EpisodeFilters(tier=MAX), ["workspace_disk"]),
        (EpisodeFilters(resource=CPU), ["cpu"]),
        (EpisodeFilters(kind=PressureKind.OOM_KILL), ["memory"]),
        (EpisodeFilters(open_only=True), ["memory", "managed_storage"]),
    ],
    ids=["window", "until-exclusive", "tier", "resource", "kind", "open-only"],
)
async def test_every_filter_narrows_the_listing_the_aggregates_and_the_peaks_alike(
    deletion_scenario: DeletionScenario, filters: EpisodeFilters, expected_resources: list[str]
):
    scenario = deletion_scenario
    _ = await _seed_report(scenario)

    listed = await dao.list_page(filters, after=None, limit=100)
    peaks = await dao.tenant_resource_peaks(filters)
    aggregates = await dao.tenant_aggregates(filters)

    assert sorted(item.episode.resource.value for item in listed) == sorted(expected_resources)
    assert sorted({peak.resource.value for peak in peaks}) == sorted(set(expected_resources))
    assert sum(sum(row.episode_counts.values()) + row.oom_count for row in aggregates) == len(listed)


async def test_the_tenant_and_agent_filters_select_their_rows(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    agents = await _seed_report(scenario)

    by_tenant = await dao.list_page(EpisodeFilters(tenant_id=scenario.foreign_tenant_id), after=None, limit=10)
    by_agent = await dao.list_page(EpisodeFilters(agent_id=agents["two"]), after=None, limit=10)

    assert [item.tenant_name for item in by_tenant] == ["Other"]
    assert [item.episode.agent_name for item in by_agent] == ["Ops"]
    assert [item.episode.agent_id for item in by_agent] == [agents["two"]]


async def test_peaks_are_grouped_by_organization_recorded_tier_and_resource(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    _ = await _seed_report(scenario)

    peaks = await dao.tenant_resource_peaks(EpisodeFilters(tenant_id=scenario.tenant_id))

    assert sorted((peak.tier.value, peak.resource.value, peak.peak_value, peak.oom) for peak in peaks) == [
        ("max", "workspace_disk", int(4.6 * GIB), False),
        ("pro", "cpu", 950, False),
        ("pro", "memory", 2 * GIB, True),
    ]


async def test_pages_walk_newest_first_with_ties_broken_by_id_and_cover_every_row_once(
    deletion_scenario: DeletionScenario,
):
    scenario = deletion_scenario
    agents = [await scenario.agent("owner", f"Agent {number}") for number in range(4)]
    for agent in agents:
        _ = await _insert(scenario, scenario.tenant_id, agent, started_at=T0)
    later = T0 + timedelta(hours=1)
    _ = await _insert(scenario, scenario.tenant_id, agents[0], resource="cpu", started_at=later, last_seen_at=later)

    seen: list[UUID] = []
    after: EpisodePosition | None = None
    while page := await dao.list_page(EpisodeFilters(), after=after, limit=2):
        assert len(page) <= 2
        seen.extend(item.episode.id for item in page)
        last = page[-1].episode
        after = EpisodePosition(last.started_at, last.id)

    everything = await dao.list_page(EpisodeFilters(), after=None, limit=100)
    assert seen == [item.episode.id for item in everything]
    assert len(seen) == len(set(seen)) == 5
    ordered = [(item.episode.started_at, item.episode.id) for item in everything]
    assert ordered == sorted(ordered, reverse=True)


async def test_the_summary_suggests_a_tier_from_episodes_recorded_under_the_current_one(
    deletion_scenario: DeletionScenario,
):
    scenario = deletion_scenario
    _ = await approve_admission_for(scenario.db, scenario.tenant_id, PRO)
    _ = await _seed_report(scenario)

    summaries = await report.tenant_summaries(EpisodeFilters())

    by_name = {row.tenant_name: row for row in summaries}
    assert (by_name["Acme"].current_tier, by_name["Acme"].suggested_tier) == (PRO, MAX)
    assert (by_name["Other"].current_tier, by_name["Other"].suggested_tier) == (None, None)
    assert by_name["Acme"].episode_counts.memory == 2
    assert by_name["Acme"].oom_count == 1


async def test_the_csv_export_pages_through_every_episode_once(
    deletion_scenario: DeletionScenario, monkeypatch: pytest.MonkeyPatch
):
    scenario = deletion_scenario
    _ = await _seed_report(scenario)
    monkeypatch.setattr(report, "EXPORT_PAGE_SIZE", 2)

    body = b"".join([chunk async for chunk in report.stream_episodes_csv(EpisodeFilters())]).decode()

    lines = body.strip().splitlines()
    assert lines[0].split(",")[:3] == ["id", "tenant_id", "tenant_name"]
    assert len(lines) == 1 + 6
    assert len({line.split(",")[0] for line in lines[1:]}) == 6


class _Guest:
    def __init__(self, agent_id: UUID, container_id: str) -> None:
        self.inspection = GuestInspection(container_id, str(agent_id), True, 1000, 1000, False, None, "", 0, 0)
        self.reading = GuestStats(container_id, 0, 0.0)

    def inspect(self, _client: object, references: Sequence[str], _timeout: float) -> dict[str, GuestInspection]:
        return dict.fromkeys(references, self.inspection)

    def stats(self, _client: object, container_ids: Sequence[str], _timeout: float) -> dict[str, GuestStats]:
        return dict.fromkeys(container_ids, self.reading)


async def test_a_page_pass_opens_extends_and_closes_episodes_and_records_a_kill_once(
    deletion_scenario: DeletionScenario, monkeypatch: pytest.MonkeyPatch
):
    scenario = deletion_scenario
    _ = await approve_admission_for(scenario.db, scenario.tenant_id, PRO)
    agent_id = await scenario.agent("owner")
    container_id = uuid4().hex * 2
    await scenario.db.execute(
        "UPDATE agents SET container_id = %(c)s WHERE id = %(id)s", {"c": container_id, "id": agent_id}
    )
    await scenario.db.execute(
        "INSERT INTO agent_storage_usage (agent_id, charged_bytes) VALUES (%(id)s, %(b)s)",
        {"id": agent_id, "b": GIB * 99 // 100},
    )
    guest = _Guest(agent_id, container_id)
    monkeypatch.setattr(pressure, "inspect_guests", guest.inspect)
    monkeypatch.setattr(pressure, "guest_stats", guest.stats)
    monkeypatch.setattr(
        pressure.agent_manager, "reacquire_docker_client", lambda: SimpleNamespace(docker_cmd=["docker"])
    )

    async def page() -> None:
        agent = await agent_dao.get(agent_id)
        assert agent is not None
        await pressure.sample_page([agent])

    guest.reading = GuestStats(container_id, 950, 50.0)
    await page()
    guest.reading = GuestStats(container_id, 980, 96.0)
    await page()
    rows = {row["resource"]: row for row in await _episodes(scenario)}
    assert set(rows) == {"memory", "cpu", "managed_storage"}
    assert (rows["memory"]["sample_count"], rows["memory"]["peak_value"], rows["memory"]["limit_value"]) == (
        2,
        980,
        1000,
    )
    assert (rows["cpu"]["peak_value"], rows["cpu"]["limit_value"]) == (960, 1000)
    assert (rows["managed_storage"]["agent_id"], rows["managed_storage"]["sample_count"]) == (agent_id, 2)

    guest.reading = GuestStats(container_id, 100, 10.0)
    await page()
    guest.inspection = GuestInspection(container_id, str(agent_id), False, 1000, 1000, True, T0, "t", 137, 2)
    await page()
    await page()

    final = await _episodes(scenario)
    assert sorted((row["resource"], row["kind"], row["ended_at"] is not None) for row in final) == [
        ("cpu", "threshold", True),
        ("managed_storage", "threshold", False),
        ("memory", "oom_kill", True),
        ("memory", "threshold", True),
    ]
    storage = next(row for row in final if row["resource"] == "managed_storage")
    assert storage["sample_count"] == 5


async def test_rerunning_the_baseline_adds_the_table_to_an_existing_database_and_keeps_its_rows(
    deletion_scenario: DeletionScenario,
):
    scenario = deletion_scenario
    agent = await scenario.agent("owner")
    _ = await _open(scenario.tenant_id, agent)

    await scenario.db.execute(BASELINE.read_text())
    assert len(await _episodes(scenario)) == 1

    await scenario.db.execute("DROP TABLE agent_resource_pressure_episodes")
    await scenario.db.execute(BASELINE.read_text())

    assert await _episodes(scenario) == []
    indexes = await scenario.rows(
        "SELECT indexname FROM pg_indexes WHERE tablename = 'agent_resource_pressure_episodes' "
        "AND schemaname = current_schema()"
    )
    assert {row["indexname"] for row in indexes} >= {
        "ux_agent_resource_pressure_open",
        "ux_agent_resource_pressure_oom_event",
        "ix_agent_resource_pressure_open_agent",
        "ix_agent_resource_pressure_started",
        "ix_agent_resource_pressure_tenant_started",
    }
    _ = await _open(scenario.tenant_id, agent)
