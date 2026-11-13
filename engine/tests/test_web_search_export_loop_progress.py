"""Progress beats of the web-search export loop on the injected clock; the DAO and the S3 sink are fakes."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.db.errors import DbError
from app.records.web_search_event import WebSearchEventRecord
from app.services.tavily import export
from worker_progress_fixtures import WorkerHTTP, worker_http

__all__ = ["worker_http"]


class ExportStore:
    def __init__(self, pending: list[WebSearchEventRecord]) -> None:
        self.pending = pending
        self.error: Exception | None = None
        self.released: list[UUID] = []
        self.exported: list[UUID] = []

    async def claim_pending_export(
        self, *, now: datetime, limit: int, stale_after: timedelta
    ) -> list[WebSearchEventRecord]:
        if self.error is not None:
            raise self.error
        return self.pending

    async def reclaim_exporting(self) -> int:
        if self.error is not None:
            raise self.error
        return 0

    async def release_export(self, event_ids: list[UUID]) -> None:
        self.released.extend(event_ids)

    async def finish_export(self, event_ids: list[UUID], *, now: datetime, delete_payloads: bool) -> None:
        self.exported.extend(event_ids)

    async def delete_older_than(self, cutoff: datetime) -> int:
        return 0

    async def delete_orphaned_payloads(self) -> int:
        return 0


class UnavailableSink:
    async def put(self, *, bucket: str, key: str, body: bytes, content_type: str) -> None:
        raise OSError("object store unavailable")


def event() -> WebSearchEventRecord:
    return WebSearchEventRecord(
        uuid4(), "search", "POST", 200, "ok", 9, "abc", export_state="pending", occurred_at=datetime.now(UTC)
    )


async def start_export_loop(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, store: ExportStore, *, enabled: bool = True
) -> asyncio.Task[object]:
    settings = SimpleNamespace(
        WEB_SEARCH_ANALYTICS_EXPORT_ENABLED=enabled,
        WEB_SEARCH_ANALYTICS_INCLUDE_RAW=False,
        WEB_SEARCH_ANALYTICS_RETENTION_DAYS=30,
        ANALYTICS_S3_BUCKET="lake",
        ANALYTICS_S3_PREFIX="web-search/",
        S3_BUCKET="",
        INSTANCE_ID="test-node",
    )
    monkeypatch.setattr(export, "web_search_event_dao", store)
    monkeypatch.setattr(export, "get_settings", lambda: settings)
    monkeypatch.setattr(asyncio, "sleep", worker_http.pacing.wait)
    task = worker_http.start_loop(
        "web_search_export", export.start_web_search_export_daemon(), budget_seconds=export.PROGRESS_BUDGET_SECONDS
    )
    await worker_http.pacing.paused()
    return task


async def test_idle_export_passes_beat_so_readiness_outlives_the_budget(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP
) -> None:
    # Given the export loop as a budgeted worker loop with nothing pending.
    task = await start_export_loop(monkeypatch, worker_http, ExportStore([]))
    # When four idle passes carry the clock past the budget.
    for _ in range(4):
        await worker_http.pacing.step()
        # Then each finished pass has beaten.
        await worker_http.ready(expected=True)
    assert not task.done()


@pytest.mark.parametrize("enabled", [True, False])
async def test_database_failures_do_not_beat_so_readiness_goes_stale_then_recovers(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, enabled: bool
) -> None:
    # Given a database rejecting the claim (export enabled) or the reclaim (export disabled).
    store = ExportStore([])
    store.error = DbError("database unavailable")
    task = await start_export_loop(monkeypatch, worker_http, store, enabled=enabled)
    # When failing passes alone carry the clock past the budget.
    for _ in range(4):
        await worker_http.pacing.step()
    # Then readiness fails while health holds and the loop keeps running.
    await worker_http.ready(expected=False)
    assert not task.done()
    # And the next pass that reads and claims successfully beats.
    store.error = None
    await worker_http.pacing.step()
    await worker_http.ready(expected=True)


async def test_object_store_failures_still_beat(monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP) -> None:
    # Given a claimed event whose upload fails on every pass.
    pending = event()
    store = ExportStore([pending])
    monkeypatch.setattr(export, "_sink", UnavailableSink)
    task = await start_export_loop(monkeypatch, worker_http, store)
    # When the failing passes carry the clock past the budget.
    for _ in range(4):
        await worker_http.pacing.step()
        # Then every pass releases the claim and still beats.
        await worker_http.ready(expected=True)
    assert store.released == [pending.id] * 5
    assert store.exported == []
    assert not task.done()
