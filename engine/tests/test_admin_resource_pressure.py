"""Platform-admin resource-pressure routes: authorization, filter parsing and the three response shapes (no live PG)."""

import csv
import io
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from app.api import admin_resource_pressure as api
from app.core.domain.users import UserRole
from app.core.security import get_current_user
from app.dao.agent_resource_pressure_episode_dao import EpisodeFilters, EpisodeListing, EpisodePosition
from app.records.agent_resource_pressure_episode import (
    AgentResourcePressureEpisodeRecord,
    PressureKind,
    PressureResource,
)
from app.records.organization_plan import OrganizationPlanId
from app.records.user import UserRecord
from app.schemas.agent_resource_pressure import (
    ResourcePressureEpisodeCounts,
    ResourcePressureEpisodePage,
    ResourcePressureTenantSummary,
)
from app.services import agent_resource_pressure_report as report

BASE = "/api/admin/resource-pressure"
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


def _user(role: UserRole) -> UserRecord:
    return UserRecord(id=uuid4(), role=role, identity=None)


@pytest.fixture
def app() -> Iterator[FastAPI]:
    scratch = FastAPI()
    scratch.include_router(api.router, prefix="/api")
    scratch.include_router(api.export_router, prefix="/api")
    scratch.dependency_overrides[get_current_user] = lambda: _user(UserRole.PLATFORM_ADMIN)
    yield scratch
    scratch.dependency_overrides.clear()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://pressure.test") as http:
        yield http


def _as(app: FastAPI, role: UserRole) -> None:
    app.dependency_overrides[get_current_user] = lambda: _user(role)


def _summary() -> ResourcePressureTenantSummary:
    return ResourcePressureTenantSummary(
        tenant_id=uuid4(),
        tenant_name="Acme",
        current_tier=OrganizationPlanId.PRO,
        episode_counts=ResourcePressureEpisodeCounts(memory=1, cpu=0, workspace_disk=0, managed_storage=0),
        oom_count=0,
        pressure_seconds=600,
        max_peak_ratio=0.95,
        agents_affected=1,
        last_seen_at=NOW,
        suggested_tier=OrganizationPlanId.MAX,
    )


@pytest.mark.parametrize("role", [UserRole.ORG_ADMIN, UserRole.AGENT_ADMIN, UserRole.MEMBER])
@pytest.mark.parametrize("path", ["/summary", "/episodes", "/export.csv"])
async def test_only_a_platform_admin_may_read(
    app: FastAPI, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, role: UserRole, path: str
):
    summaries, pages, stream = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(api, "tenant_summaries", summaries)
    monkeypatch.setattr(api, "episode_page", pages)
    monkeypatch.setattr(api, "stream_episodes_csv", stream)
    _as(app, role)

    response = await client.get(BASE + path)

    assert response.status_code == 403
    summaries.assert_not_awaited()
    pages.assert_not_awaited()
    stream.assert_not_called()


async def test_the_summary_passes_every_filter_through_and_returns_the_rows(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    tenant_id = uuid4()
    summaries = AsyncMock(return_value=[_summary()])
    monkeypatch.setattr(api, "tenant_summaries", summaries)

    response = await client.get(
        BASE + "/summary",
        params={
            "since": "2026-11-01T00:00:00Z",
            "until": "2026-11-13T00:00:00+00:00",
            "tier": "pro",
            "resource": "workspace_disk",
            "tenant_id": str(tenant_id),
        },
    )

    assert response.status_code == 200
    (row,) = response.json()
    assert row["suggested_tier"] == "max"
    assert row["episode_counts"] == {"memory": 1, "cpu": 0, "workspace_disk": 0, "managed_storage": 0}
    summaries.assert_awaited_once_with(
        EpisodeFilters(
            since=datetime(2026, 11, 1, tzinfo=UTC),
            until=datetime(2026, 11, 13, tzinfo=UTC),
            tier=OrganizationPlanId.PRO,
            resource=PressureResource.WORKSPACE_DISK,
            tenant_id=tenant_id,
        )
    )


async def test_without_filters_nothing_is_filtered_and_naive_times_are_utc(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    summaries = AsyncMock(return_value=[])
    monkeypatch.setattr(api, "tenant_summaries", summaries)

    unfiltered = await client.get(BASE + "/summary")
    naive = await client.get(BASE + "/summary", params={"since": "2026-11-01T00:00:00"})

    assert (unfiltered.status_code, unfiltered.json()) == (200, [])
    assert naive.status_code == 200
    assert [call.args[0] for call in summaries.await_args_list] == [
        EpisodeFilters(),
        EpisodeFilters(since=datetime(2026, 11, 1, tzinfo=UTC)),
    ]


@pytest.mark.parametrize("path", ["/summary", "/episodes", "/export.csv"])
async def test_an_empty_or_inverted_window_is_a_400_before_any_read(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, path: str
):
    summaries, pages, stream = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(api, "tenant_summaries", summaries)
    monkeypatch.setattr(api, "episode_page", pages)
    monkeypatch.setattr(api, "stream_episodes_csv", stream)

    equal = await client.get(BASE + path, params={"since": "2026-11-01T00:00:00Z", "until": "2026-11-01T00:00:00Z"})
    inverted = await client.get(BASE + path, params={"since": "2026-11-02T00:00:00Z", "until": "2026-11-01T00:00:00Z"})

    assert (equal.status_code, inverted.status_code) == (400, 400)
    assert equal.json() == {"detail": "until must be after since"}
    summaries.assert_not_awaited()
    pages.assert_not_awaited()
    stream.assert_not_called()


@pytest.mark.parametrize(
    "params",
    [
        {"tier": "platinum"},
        {"resource": "network"},
        {"tenant_id": "nope"},
        {"since": "yesterday"},
    ],
)
@pytest.mark.parametrize("path", ["/summary", "/episodes", "/export.csv"])
async def test_a_malformed_filter_is_a_422(client: httpx.AsyncClient, path: str, params: dict[str, str]):
    assert (await client.get(BASE + path, params=params)).status_code == 422


async def test_episodes_take_the_extra_filters_a_limit_and_a_cursor(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    agent_id, tenant_id = uuid4(), uuid4()
    page = AsyncMock(return_value=ResourcePressureEpisodePage(items=(), next_cursor="next"))
    monkeypatch.setattr(api, "episode_page", page)

    response = await client.get(
        BASE + "/episodes",
        params={
            "tenant_id": str(tenant_id),
            "agent_id": str(agent_id),
            "kind": "oom_kill",
            "open_only": "true",
            "limit": "10",
            "cursor": "abc",
        },
    )

    assert (response.status_code, response.json()) == (200, {"items": [], "next_cursor": "next"})
    page.assert_awaited_once_with(
        EpisodeFilters(tenant_id=tenant_id, agent_id=agent_id, kind=PressureKind.OOM_KILL, open_only=True),
        cursor="abc",
        limit=10,
    )


async def test_episodes_default_to_fifty_unfiltered_and_bound_the_limit(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    page = AsyncMock(return_value=ResourcePressureEpisodePage(items=(), next_cursor=None))
    monkeypatch.setattr(api, "episode_page", page)

    assert (await client.get(BASE + "/episodes")).status_code == 200
    page.assert_awaited_once_with(EpisodeFilters(), cursor=None, limit=50)
    for limit in ("0", "201", "-1", "ten"):
        assert (await client.get(BASE + "/episodes", params={"limit": limit})).status_code == 422
    assert (await client.get(BASE + "/episodes", params={"limit": "200"})).status_code == 200


class Listing:
    def __init__(self, listings: list[EpisodeListing]) -> None:
        self.listings: list[EpisodeListing] = listings
        self.calls: list[tuple[EpisodeFilters, EpisodePosition | None, int]] = []

    async def list_page(
        self, filters: EpisodeFilters, *, after: EpisodePosition | None, limit: int
    ) -> list[EpisodeListing]:
        self.calls.append((filters, after, limit))
        return self.listings[: limit if after is None else 0]


def _listing(name: str, *, tenant: str = "Acme", agent_id: UUID | None = None) -> EpisodeListing:
    return EpisodeListing(
        AgentResourcePressureEpisodeRecord(
            id=uuid4(),
            tenant_id=uuid4(),
            agent_id=agent_id,
            agent_name=name,
            tier=OrganizationPlanId.MAX,
            resource=PressureResource.CPU,
            limit_value=1500,
            peak_value=1450,
            peak_ratio=1450 / 1500,
            threshold_ratio=0.9,
            started_at=NOW,
            last_seen_at=NOW + timedelta(minutes=5),
            detail={"tier_limit": 1500},
        ),
        tenant,
    )


async def test_the_real_episode_page_serializes_rows_and_rejects_a_bad_cursor(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    agent_id = uuid4()
    store = Listing([_listing("Ops", agent_id=agent_id)])
    monkeypatch.setattr(report, "agent_resource_pressure_episode_dao", store)

    page = await client.get(BASE + "/episodes", params={"resource": "cpu"})
    bad = await client.get(BASE + "/episodes", params={"cursor": "garbage"})

    assert page.status_code == 200
    (item,) = page.json()["items"]
    assert item["agent_id"] == str(agent_id)
    assert (item["tenant_name"], item["tier"], item["resource"], item["kind"]) == ("Acme", "max", "cpu", "threshold")
    assert (item["limit_value"], item["peak_value"], item["sample_count"], item["ended_at"]) == (1500, 1450, 1, None)
    assert item["detail"] == {"tier_limit": 1500}
    assert (bad.status_code, bad.json()) == (400, {"detail": "Invalid cursor"})
    assert store.calls[0][0] == EpisodeFilters(resource=PressureResource.CPU)


async def test_the_csv_export_streams_a_header_and_the_matching_rows_as_an_attachment(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    store = Listing([_listing("Ops"), _listing("=2+2", tenant="Beta")])
    monkeypatch.setattr(report, "agent_resource_pressure_episode_dao", store)

    response = await client.get(BASE + "/export.csv", params={"tier": "max", "open_only": "true"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["content-disposition"] == 'attachment; filename="resource-pressure-episodes.csv"'
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows[0] == list(report.CSV_COLUMNS)
    assert [(row[2], row[4], row[6]) for row in rows[1:]] == [("Acme", "Ops", "cpu"), ("Beta", "'=2+2", "cpu")]
    assert store.calls[0][0] == EpisodeFilters(tier=OrganizationPlanId.MAX, open_only=True)


def test_the_three_routes_are_admin_tagged_and_published_under_the_api_prefix():
    from app import main

    routes = [route for route in (*api.router.routes, *api.export_router.routes) if isinstance(route, APIRoute)]

    assert sorted(route.path for route in routes) == [
        "/admin/resource-pressure/episodes",
        "/admin/resource-pressure/export.csv",
        "/admin/resource-pressure/summary",
    ]
    assert all(route.tags == ["admin"] for route in routes)
    assert {path for path in main.app.openapi()["paths"] if path.startswith(BASE)} == {
        f"{BASE}/summary",
        f"{BASE}/episodes",
        f"{BASE}/export.csv",
    }
