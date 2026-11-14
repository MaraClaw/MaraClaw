"""Resource-pressure reporting: tier suggestion, per-organization summary, cursor pages and CSV rows."""

import csv
import io
import json
from base64 import urlsafe_b64encode
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app.dao.agent_resource_pressure_episode_dao import (
    EpisodeFilters,
    EpisodeListing,
    EpisodePosition,
    TenantPressureAggregate,
    TenantResourcePeak,
)
from app.records.agent_resource_pressure_episode import (
    AgentResourcePressureEpisodeRecord,
    PressureKind,
    PressureResource,
)
from app.records.organization_plan import OrganizationPlanId
from app.services import agent_resource_pressure_report as report
from app.services.organization_plans import plan_for

GIB = 1024**3
PRO, MAX, ULTRA = OrganizationPlanId.PRO, OrganizationPlanId.MAX, OrganizationPlanId.ULTRA
MEMORY, CPU = PressureResource.MEMORY, PressureResource.CPU
DISK, STORAGE = PressureResource.WORKSPACE_DISK, PressureResource.MANAGED_STORAGE
THRESHOLD = 0.9
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


def test_the_tiers_are_ordered_from_smallest_to_largest():
    assert report.TIERS == (PRO, MAX, ULTRA)


@pytest.mark.parametrize(
    ("resource", "expected"),
    [(MEMORY, 2 * GIB), (CPU, 1500), (DISK, 2_684_354_560), (STORAGE, GIB)],
)
def test_each_resource_reads_its_plan_field_in_the_unit_episodes_use(resource: PressureResource, expected: int):
    assert report.resource_limit(plan_for(PRO), resource) == expected


@pytest.mark.parametrize(
    ("current", "resource", "peak", "oom", "expected"),
    [
        (PRO, MEMORY, int(1.9 * GIB), False, MAX),
        (PRO, MEMORY, int(2.9 * GIB), False, ULTRA),
        (PRO, MEMORY, int(3.7 * GIB), False, None),
        (MAX, MEMORY, int(3.1 * GIB), False, ULTRA),
        (ULTRA, MEMORY, 4 * GIB, False, None),
        (PRO, CPU, 1300, False, None),
        (PRO, CPU, 1400, False, MAX),
        (PRO, CPU, 1900, False, ULTRA),
        (PRO, CPU, 2800, False, None),
        (PRO, DISK, int(2.4 * GIB), False, MAX),
        (MAX, DISK, int(4.9 * GIB), False, ULTRA),
        (PRO, STORAGE, int(0.95 * GIB), False, MAX),
        (PRO, STORAGE, 5 * GIB, False, ULTRA),
        (PRO, MEMORY, 2 * GIB, True, MAX),
        (MAX, MEMORY, 3 * GIB, True, ULTRA),
        (ULTRA, MEMORY, 4 * GIB, True, None),
        pytest.param(MAX, MEMORY, int(1.95 * GIB), False, None, id="stale-pro-limit-peak-fits-max"),
        pytest.param(MAX, MEMORY, 2 * GIB, True, None, id="stale-pro-limit-oom-fits-max"),
        pytest.param(MAX, CPU, 1000, False, None, id="stale-pro-cpu-ceiling-fits-max"),
        pytest.param(MAX, MEMORY, int(2.8 * GIB), False, ULTRA, id="real-pressure-at-max"),
    ],
)
def test_a_resource_suggests_the_smallest_larger_tier_that_keeps_the_peak_under_the_threshold(
    current: OrganizationPlanId, resource: PressureResource, peak: int, oom: bool, expected: OrganizationPlanId | None
):
    demand = report.ResourceDemand(resource, peak, oom)

    assert report.suggest_resource_tier(current, demand, THRESHOLD) is expected


def test_the_peak_must_be_strictly_below_the_threshold_of_the_candidate_limit():
    max_memory = int(plan_for(MAX).guest_memory_bytes)
    inside = int(THRESHOLD * max_memory)
    assert inside < THRESHOLD * max_memory < inside + 1

    fits = report.ResourceDemand(MEMORY, inside)
    just_over = report.ResourceDemand(MEMORY, inside + 1)

    assert report.suggest_resource_tier(PRO, fits, THRESHOLD) is MAX
    assert report.suggest_resource_tier(PRO, just_over, THRESHOLD) is ULTRA


def test_the_threshold_is_the_callers_not_a_constant():
    demand = report.ResourceDemand(MEMORY, 3 * GIB)

    assert report.suggest_resource_tier(PRO, demand, 1.0) is ULTRA
    assert report.suggest_resource_tier(PRO, report.ResourceDemand(MEMORY, int(2.9 * GIB)), 1.0) is MAX


def test_the_organization_needs_the_largest_suggestion_of_its_resources():
    demands = [report.ResourceDemand(MEMORY, int(1.9 * GIB)), report.ResourceDemand(DISK, int(4.8 * GIB))]

    assert report.suggest_tier(PRO, demands, THRESHOLD) is ULTRA
    assert report.suggest_tier(PRO, reversed(demands), THRESHOLD) is ULTRA


def test_a_resource_nothing_can_relieve_does_not_hide_one_that_can_be():
    demands = [report.ResourceDemand(CPU, 5000), report.ResourceDemand(MEMORY, int(1.9 * GIB))]

    assert report.suggest_tier(PRO, demands, THRESHOLD) is MAX


@pytest.mark.parametrize("current", [PRO, MAX, ULTRA])
def test_no_demand_or_nothing_that_fits_suggests_nothing(current: OrganizationPlanId):
    assert report.suggest_tier(current, [], THRESHOLD) is None
    assert report.suggest_tier(current, [report.ResourceDemand(CPU, 10_000)], THRESHOLD) is None


class FakeEpisodes:
    def __init__(self, aggregates: list[TenantPressureAggregate], peaks: list[TenantResourcePeak]) -> None:
        self.aggregates: list[TenantPressureAggregate] = aggregates
        self.peaks: list[TenantResourcePeak] = peaks
        self.filters: list[EpisodeFilters] = []

    async def tenant_aggregates(self, filters: EpisodeFilters) -> list[TenantPressureAggregate]:
        self.filters.append(filters)
        return self.aggregates

    async def tenant_resource_peaks(self, filters: EpisodeFilters) -> list[TenantResourcePeak]:
        self.filters.append(filters)
        return self.peaks


class FakeAdmissions:
    def __init__(self, linked: dict[UUID, list[OrganizationPlanId]]) -> None:
        self.linked: dict[UUID, list[OrganizationPlanId]] = linked
        self.asked: list[list[UUID]] = []

    async def approved_tiers_for_tenants(self, tenant_ids: Sequence[UUID]) -> dict[UUID, list[OrganizationPlanId]]:
        self.asked.append(list(tenant_ids))
        return self.linked


def _aggregate(tenant_id: UUID, name: str = "Acme") -> TenantPressureAggregate:
    return TenantPressureAggregate(
        tenant_id=tenant_id,
        tenant_name=name,
        episode_counts={MEMORY: 3, CPU: 0, DISK: 1, STORAGE: 2},
        oom_count=1,
        pressure_seconds=900,
        max_peak_ratio=1.0,
        agents_affected=2,
        last_seen_at=NOW,
    )


def _peak(tenant_id: UUID, tier: OrganizationPlanId, resource: PressureResource, value: int, oom: bool = False):
    return TenantResourcePeak(tenant_id, tier, resource, value, oom)


async def test_a_summary_row_carries_the_aggregates_the_current_tier_and_the_suggestion(
    monkeypatch: pytest.MonkeyPatch,
):
    tenant_id = uuid4()
    episodes = FakeEpisodes([_aggregate(tenant_id)], [_peak(tenant_id, PRO, MEMORY, int(1.9 * GIB))])
    monkeypatch.setattr(report, "agent_resource_pressure_episode_dao", episodes)
    monkeypatch.setattr(report, "organization_admission_dao", FakeAdmissions({tenant_id: [PRO]}))
    filters = EpisodeFilters(tenant_id=tenant_id)

    (row,) = await report.tenant_summaries(filters)

    assert row.model_dump(mode="json") == {
        "tenant_id": str(tenant_id),
        "tenant_name": "Acme",
        "current_tier": "pro",
        "episode_counts": {"memory": 3, "cpu": 0, "workspace_disk": 1, "managed_storage": 2},
        "oom_count": 1,
        "pressure_seconds": 900,
        "max_peak_ratio": 1.0,
        "agents_affected": 2,
        "last_seen_at": NOW.isoformat().replace("+00:00", "Z"),
        "suggested_tier": "max",
    }
    assert episodes.filters == [filters, filters]


async def test_only_episodes_recorded_under_the_current_tier_drive_the_suggestion(monkeypatch: pytest.MonkeyPatch):
    tenant_id = uuid4()
    peaks = [
        _peak(tenant_id, PRO, MEMORY, int(1.9 * GIB), oom=True),
        _peak(tenant_id, MAX, DISK, int(4.8 * GIB)),
        _peak(uuid4(), MAX, CPU, 1400),
    ]
    monkeypatch.setattr(report, "agent_resource_pressure_episode_dao", FakeEpisodes([_aggregate(tenant_id)], peaks))
    monkeypatch.setattr(report, "organization_admission_dao", FakeAdmissions({tenant_id: [MAX]}))

    (row,) = await report.tenant_summaries(EpisodeFilters())

    assert (row.current_tier, row.suggested_tier) == (MAX, ULTRA)


@pytest.mark.parametrize("links", [[], [PRO, MAX]], ids=["unlinked", "ambiguous"])
async def test_an_organization_without_exactly_one_approved_tier_gets_no_suggestion(
    monkeypatch: pytest.MonkeyPatch, links: list[OrganizationPlanId]
):
    tenant_id = uuid4()
    episodes = FakeEpisodes([_aggregate(tenant_id)], [_peak(tenant_id, PRO, MEMORY, int(1.9 * GIB))])
    monkeypatch.setattr(report, "agent_resource_pressure_episode_dao", episodes)
    monkeypatch.setattr(report, "organization_admission_dao", FakeAdmissions({tenant_id: links} if links else {}))

    (row,) = await report.tenant_summaries(EpisodeFilters())

    assert (row.current_tier, row.suggested_tier) == (None, None)


async def test_tiers_are_read_in_one_batch_and_nothing_is_read_without_episodes(monkeypatch: pytest.MonkeyPatch):
    first, second = uuid4(), uuid4()
    admissions = FakeAdmissions({first: [PRO], second: [ULTRA]})
    episodes = FakeEpisodes([_aggregate(first), _aggregate(second, "Zed")], [])
    monkeypatch.setattr(report, "agent_resource_pressure_episode_dao", episodes)
    monkeypatch.setattr(report, "organization_admission_dao", admissions)

    rows = await report.tenant_summaries(EpisodeFilters())

    assert [(row.tenant_name, row.current_tier, row.suggested_tier) for row in rows] == [
        ("Acme", PRO, None),
        ("Zed", ULTRA, None),
    ]
    assert admissions.asked == [[first, second]]

    episodes.aggregates = []
    admissions.asked.clear()
    assert await report.tenant_summaries(EpisodeFilters()) == []
    assert admissions.asked == []


def _cursor(**fields: object) -> str:
    return urlsafe_b64encode(json.dumps(fields).encode()).decode().rstrip("=")


def test_a_cursor_round_trips_its_position():
    identifier = uuid4()
    cursor = report.EpisodeCursor(version=1, started_at=NOW, id=identifier)

    decoded = report.EpisodeCursor.decode(cursor.encode())

    assert (decoded.started_at, decoded.id) == (NOW, identifier)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "!!!",
        "x" * (report.MAX_CURSOR_LENGTH + 1),
        _cursor(version=2, started_at=NOW.isoformat(), id=str(uuid4())),
        _cursor(version=1, started_at="2026-11-13T12:00:00", id=str(uuid4())),
        _cursor(version=1, started_at=NOW.isoformat(), id="not-a-uuid"),
        _cursor(version=1, started_at=NOW.isoformat(), id=str(uuid4()), extra=1),
        _cursor(version=True, started_at=NOW.isoformat(), id=str(uuid4())),
    ],
)
def test_a_malformed_or_foreign_cursor_is_a_400(value: str):
    with pytest.raises(HTTPException) as refused:
        report.EpisodeCursor.decode(value)

    assert refused.value.status_code == 400
    assert refused.value.detail == "Invalid cursor"


def _episode(
    *,
    started: datetime = NOW,
    agent_id: UUID | None = None,
    name: str = "Ops",
    kind: PressureKind = PressureKind.THRESHOLD,
    ended: bool = False,
) -> AgentResourcePressureEpisodeRecord:
    return AgentResourcePressureEpisodeRecord(
        id=uuid4(),
        tenant_id=uuid4(),
        agent_id=agent_id,
        agent_name=name,
        tier=PRO,
        resource=MEMORY,
        kind=kind,
        limit_value=2 * GIB,
        peak_value=int(1.9 * GIB),
        peak_ratio=0.95,
        threshold_ratio=0.9,
        started_at=started,
        last_seen_at=started + timedelta(minutes=10),
        ended_at=started + timedelta(minutes=15) if ended else None,
        sample_count=3,
        detail={"tier_limit": 2 * GIB},
    )


class FakeListing:
    def __init__(self, listings: list[EpisodeListing]) -> None:
        self.listings: list[EpisodeListing] = listings
        self.calls: list[tuple[EpisodeFilters, EpisodePosition | None, int]] = []

    async def list_page(
        self, filters: EpisodeFilters, *, after: EpisodePosition | None, limit: int
    ) -> list[EpisodeListing]:
        self.calls.append((filters, after, limit))
        if after is None:
            start = 0
        else:
            start = next(i for i, item in enumerate(self.listings) if item.episode.id == after.id) + 1
        return self.listings[start : start + limit]


def _listings(count: int) -> list[EpisodeListing]:
    return [EpisodeListing(_episode(started=NOW - timedelta(minutes=index)), f"Org {index}") for index in range(count)]


async def test_a_page_asks_for_one_more_row_and_hands_out_a_cursor_only_when_there_is_more(
    monkeypatch: pytest.MonkeyPatch,
):
    store = FakeListing(_listings(5))
    monkeypatch.setattr(report, "agent_resource_pressure_episode_dao", store)
    filters = EpisodeFilters(open_only=True)

    first = await report.episode_page(filters, cursor=None, limit=2)

    assert [item.tenant_name for item in first.items] == ["Org 0", "Org 1"]
    assert first.next_cursor is not None
    second = await report.episode_page(filters, cursor=first.next_cursor, limit=2)
    assert [item.tenant_name for item in second.items] == ["Org 2", "Org 3"]
    assert second.next_cursor is not None
    last = await report.episode_page(filters, cursor=second.next_cursor, limit=2)
    assert [item.tenant_name for item in last.items] == ["Org 4"]
    assert last.next_cursor is None
    assert [(call[0], call[2]) for call in store.calls] == [(filters, 3)] * 3
    boundary = store.listings[1].episode
    assert store.calls[1][1] == EpisodePosition(boundary.started_at, boundary.id)


async def test_an_exactly_full_last_page_has_no_cursor_and_a_bad_cursor_reads_nothing(monkeypatch: pytest.MonkeyPatch):
    store = FakeListing(_listings(2))
    monkeypatch.setattr(report, "agent_resource_pressure_episode_dao", store)

    page = await report.episode_page(EpisodeFilters(), cursor=None, limit=2)
    with pytest.raises(HTTPException):
        _ = await report.episode_page(EpisodeFilters(), cursor="garbage", limit=2)

    assert page.next_cursor is None
    assert len(store.calls) == 1


def _rows(body: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(body.decode("utf-8"))))


async def _export(filters: EpisodeFilters | None = None) -> list[list[str]]:
    body = b"".join([chunk async for chunk in report.stream_episodes_csv(filters or EpisodeFilters())])
    return _rows(body)


async def test_the_export_is_the_header_then_one_row_per_episode(monkeypatch: pytest.MonkeyPatch):
    agent_id = uuid4()
    listing = EpisodeListing(_episode(agent_id=agent_id, ended=True), "Acme Corp")
    open_one = EpisodeListing(_episode(agent_id=None, kind=PressureKind.THRESHOLD), "Beta")
    monkeypatch.setattr(report, "agent_resource_pressure_episode_dao", FakeListing([listing, open_one]))

    rows = await _export()

    assert rows[0] == list(report.CSV_COLUMNS)
    episode = listing.episode
    assert rows[1] == [
        str(episode.id),
        str(episode.tenant_id),
        "Acme Corp",
        str(agent_id),
        "Ops",
        "pro",
        "memory",
        "threshold",
        str(2 * GIB),
        str(int(1.9 * GIB)),
        "0.95",
        "0.9",
        NOW.isoformat(),
        (NOW + timedelta(minutes=10)).isoformat(),
        (NOW + timedelta(minutes=15)).isoformat(),
        "3",
        '{"tier_limit":2147483648}',
    ]
    assert rows[2][3] == ""
    assert rows[2][14] == ""
    assert len(rows) == 3


async def test_text_that_a_person_chose_cannot_become_a_spreadsheet_formula(monkeypatch: pytest.MonkeyPatch):
    listing = EpisodeListing(_episode(name='=HYPERLINK("http://evil","x")'), "@SUM(A1)")
    monkeypatch.setattr(report, "agent_resource_pressure_episode_dao", FakeListing([listing]))

    rows = await _export()

    assert rows[1][2] == "'@SUM(A1)"
    assert rows[1][4] == '\'=HYPERLINK("http://evil","x")'
    assert report.csv_text_cell("plain name") == "plain name"
    assert report.csv_text_cell("  -2+3") == "'  -2+3"
    assert report.csv_text_cell("\tx") == "'\tx"


async def test_the_export_reads_keyset_pages_until_a_short_one_and_forwards_the_filters(
    monkeypatch: pytest.MonkeyPatch,
):
    store = FakeListing(_listings(5))
    monkeypatch.setattr(report, "agent_resource_pressure_episode_dao", store)
    monkeypatch.setattr(report, "EXPORT_PAGE_SIZE", 2)
    filters = EpisodeFilters(tier=PRO, resource=MEMORY)

    rows = await _export(filters)

    assert [row[2] for row in rows[1:]] == [f"Org {index}" for index in range(5)]
    assert [call[2] for call in store.calls] == [2, 2, 2]
    assert [call[0] for call in store.calls] == [filters] * 3
    assert [call[1] for call in store.calls] == [
        None,
        EpisodePosition(store.listings[1].episode.started_at, store.listings[1].episode.id),
        EpisodePosition(store.listings[3].episode.started_at, store.listings[3].episode.id),
    ]


async def test_an_empty_export_is_just_the_header(monkeypatch: pytest.MonkeyPatch):
    store = FakeListing([])
    monkeypatch.setattr(report, "agent_resource_pressure_episode_dao", store)

    assert await _export() == [list(report.CSV_COLUMNS)]
    assert len(store.calls) == 1
