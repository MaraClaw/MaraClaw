from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import anyio
import pytest
from tests.test_transaction_boundaries import pool
from tests.transaction_pool_fakes import CountingPool

from app.dao.okr_dao import company_report_dao, member_daily_report_dao
from app.db.session import get_connection
from app.records.llm import LLMModelRecord
from app.records.okr import CompanyReportRecord, MemberDailyReportRecord
from app.services import okr_report_generation as generation, okr_reporting as reporting

__all__ = ["pool"]

TENANT = uuid4()
MEMBER = uuid4()
DAY = date(2026, 11, 2)
CLOCK = datetime(2026, 11, 8, tzinfo=UTC)


class ReportStore:
    def __init__(self) -> None:
        self.reports: dict[tuple[str, date, date], CompanyReportRecord] = {}
        self.members: list[MemberDailyReportRecord] = []
        self.clock = CLOCK
        self.phases: list[bool] = []

    async def lock(self, _tenant: UUID) -> None:
        assert get_connection() is not None
        self.phases.append(True)

    async def get(
        self, _tenant: UUID, *, report_type: str, period_start: date, period_end: date
    ) -> CompanyReportRecord | None:
        row = self.reports.get((report_type, period_start, period_end))
        return replace(row) if row else None

    async def publish(self, row: CompanyReportRecord, *, expected_updated_at: datetime | None) -> CompanyReportRecord:
        assert get_connection() is not None
        key = (row.report_type, row.period_start, row.period_end)
        current = self.reports.get(key)
        if current is not None and current.updated_at != expected_updated_at:
            return replace(current)
        self.clock += timedelta(microseconds=1)
        saved = replace(row, updated_at=self.clock, generated_at=self.clock)
        self.reports[key] = saved
        return replace(saved)

    async def watermark(self, _tenant: UUID, start: date, end: date) -> list[tuple[UUID, datetime | None, date]]:
        return sorted(
            (row.id, row.updated_at, row.report_date) for row in self.members if start <= row.report_date <= end
        )

    async def daily(self, _tenant: UUID, day: date) -> list[MemberDailyReportRecord]:
        return [replace(row) for row in self.members if row.report_date == day]

    async def sources(
        self, kind: str, _tenant: UUID, *, period_start: date, period_end: date
    ) -> list[CompanyReportRecord]:
        return [
            replace(row)
            for row in self.reports.values()
            if row.report_type == kind and period_start <= row.period_start <= period_end
        ]

    async def member_get(
        self, _tenant: UUID, *, member_type: str, member_id: UUID, report_date: date
    ) -> MemberDailyReportRecord | None:
        return next(
            (
                replace(row)
                for row in self.members
                if row.member_id == member_id and row.member_type == member_type and row.report_date == report_date
            ),
            None,
        )

    async def member_create(self, *, obj_in: dict[str, object]) -> MemberDailyReportRecord:
        row = MemberDailyReportRecord.from_row({"id": uuid4(), **obj_in, "updated_at": self.clock})
        self.members.append(row)
        return replace(row)

    async def member_update(
        self, *, db_obj: MemberDailyReportRecord, obj_in: dict[str, object]
    ) -> MemberDailyReportRecord:
        # Given a database transaction whose clock predates the snapshot
        self.clock -= timedelta(days=1)
        row = MemberDailyReportRecord.from_row(
            {
                "id": db_obj.id,
                "tenant_id": db_obj.tenant_id,
                "member_type": db_obj.member_type,
                "member_id": db_obj.member_id,
                "report_date": db_obj.report_date,
                **obj_in,
                "updated_at": self.clock,
            }
        )
        self.members = [row if item.id == row.id else item for item in self.members]
        return replace(row)

    async def mark(self, _tenant: UUID, day: date) -> None:
        for key, row in list(self.reports.items()):
            if row.period_start <= day <= row.period_end:
                self.clock += timedelta(microseconds=1)
                self.reports[key] = replace(row, needs_refresh=True, updated_at=self.clock)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, pool: CountingPool) -> ReportStore:
    from functools import partial

    state = ReportStore()
    monkeypatch.setattr(company_report_dao, "lock_reporting", state.lock)
    monkeypatch.setattr(company_report_dao, "get_for_period", state.get)
    monkeypatch.setattr(company_report_dao, "publish", state.publish)
    monkeypatch.setattr(company_report_dao, "mark_needs_refresh_for_day", state.mark)
    monkeypatch.setattr(company_report_dao, "list_dailies_in_range", partial(state.sources, "daily"))
    monkeypatch.setattr(company_report_dao, "list_weeklies_in_range", partial(state.sources, "weekly"))
    monkeypatch.setattr(member_daily_report_dao, "source_watermark", state.watermark)
    monkeypatch.setattr(member_daily_report_dao, "list_for_date", state.daily)
    monkeypatch.setattr(member_daily_report_dao, "get_for_member_date", state.member_get)
    monkeypatch.setattr(member_daily_report_dao, "create", state.member_create)
    monkeypatch.setattr(member_daily_report_dao, "update", state.member_update)
    monkeypatch.setattr(
        reporting,
        "list_tracked_okr_members",
        AsyncMock(
            return_value=[
                reporting.CompanyMember("user", MEMBER, "Ada", None, "People"),
            ]
        ),
    )
    monkeypatch.setattr(
        reporting, "_resolve_report_models", AsyncMock(return_value=reporting.ResolvedReportModels(None, None, None))
    )
    return state


def report(kind: str, start: date, end: date, *, dirty: bool = False) -> CompanyReportRecord:
    return CompanyReportRecord(
        id=uuid4(),
        tenant_id=TENANT,
        report_type=kind,
        period_start=start,
        period_end=end,
        content="prior content",
        needs_refresh=dirty,
        updated_at=CLOCK,
    )


@pytest.mark.parametrize("kind", ["daily", "weekly", "monthly"])
@pytest.mark.parametrize("existing", [False, True])
async def test_member_committing_during_model_keeps_target_dirty(
    kind: str,
    existing: bool,
    store: ReportStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = DAY.replace(day=1) if kind == "monthly" else DAY
    end = date(2026, 11, 30) if kind == "monthly" else DAY + timedelta(days=6) if kind == "weekly" else DAY
    if existing:
        store.reports[(kind, start, end)] = report(kind, start, end)

    async def model(*_args: object, **_kwargs: object) -> str:
        assert get_connection() is None
        await reporting.upsert_member_daily_report(TENANT, "user", MEMBER, DAY, "new progress")
        return "snapshot content"

    monkeypatch.setattr(reporting, "_generate_llm_report_content", model)
    saved = await generation.generate_company_report(TENANT, kind, DAY)
    assert saved.needs_refresh is True
    assert len(store.members) == 1
    assert len(store.reports) == 1


@pytest.mark.parametrize("kind", ["weekly", "monthly"])
@pytest.mark.parametrize("mutation", ["flagged", "changed", "removed", "added"])
async def test_rollup_checks_each_source_and_refresh_flag(
    kind: str,
    mutation: str,
    store: ReportStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_kind = "daily" if kind == "weekly" else "weekly"
    source = report(source_kind, DAY, DAY, dirty=mutation == "flagged")
    key = (source_kind, DAY, DAY)
    store.reports[key] = source

    async def model(*_args: object, **_kwargs: object) -> str:
        if mutation == "changed":
            store.reports[key] = replace(source, updated_at=CLOCK - timedelta(days=1))
        elif mutation == "removed":
            del store.reports[key]
        elif mutation == "added":
            next_day = DAY + timedelta(days=1)
            store.reports[(source_kind, next_day, next_day)] = report(source_kind, next_day, next_day)
        return "rollup"

    monkeypatch.setattr(reporting, "_generate_llm_report_content", model)
    saved = await generation.generate_company_report(TENANT, kind, DAY)
    assert saved.needs_refresh is True


@pytest.mark.parametrize("kind", ["daily", "weekly", "monthly"])
async def test_unchanged_sources_publish_fresh(kind: str, store: ReportStore) -> None:
    saved = await generation.generate_company_report(TENANT, kind, DAY)
    assert saved.needs_refresh is False
    assert saved.content.startswith("# Company")
    assert saved.updated_at is not None
    assert store.phases == [True, True]


@pytest.mark.parametrize("existing", [False, True])
async def test_older_generation_cannot_overwrite_newer_publish(
    existing: bool,
    store: ReportStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if existing:
        store.reports[("daily", DAY, DAY)] = report("daily", DAY, DAY)
    entered, release = anyio.Event(), anyio.Event()
    calls = 0
    results: list[CompanyReportRecord] = []

    async def model(*_args: object, **_kwargs: object) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            with anyio.fail_after(3):
                await release.wait()
            return "older snapshot"
        return "newer snapshot"

    async def old() -> None:
        results.append(await generation.generate_company_report(TENANT, "daily", DAY))

    latest: CompanyReportRecord | None = None
    monkeypatch.setattr(reporting, "_generate_llm_report_content", model)
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(old)
        with anyio.fail_after(3):
            await entered.wait()
        try:
            await reporting.upsert_member_daily_report(TENANT, "user", MEMBER, DAY, "new snapshot input")
            latest = await generation.generate_company_report(TENANT, "daily", DAY)
        finally:
            release.set()
    assert latest is not None
    assert latest.content == "newer snapshot"
    assert results[0] == latest
    assert len(store.reports) == 1


async def test_primary_and_fallback_failure_preserve_prior_content(
    store: ReportStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prior = report("daily", DAY, DAY, dirty=True)
    store.reports[("daily", DAY, DAY)] = prior
    primary = LLMModelRecord(id=uuid4(), label="primary", provider="openai", model="primary", api_key_encrypted="")
    fallback = replace(primary, id=uuid4(), model="fallback")
    monkeypatch.setattr(
        reporting,
        "_resolve_report_models",
        AsyncMock(
            return_value=reporting.ResolvedReportModels(primary=primary, fallback=fallback, okr_agent_id=None),
        ),
    )
    failed = AsyncMock(side_effect=TimeoutError("provider timeout"))
    monkeypatch.setattr(reporting, "chat_complete", failed)
    with pytest.raises(RuntimeError, match="All configured OKR report models failed"):
        await generation.generate_company_report(TENANT, "daily", DAY)
    assert failed.await_count == 2
    assert store.reports[("daily", DAY, DAY)] == prior
    assert get_connection() is None


@pytest.mark.parametrize("kind", ["daily", "weekly", "monthly"])
async def test_member_watermark_detects_earlier_database_timestamp(
    kind: str,
    store: ReportStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await reporting.upsert_member_daily_report(TENANT, "user", MEMBER, DAY, "before")
    before = store.members[0].updated_at

    async def model(*_args: object, **_kwargs: object) -> str:
        await reporting.upsert_member_daily_report(TENANT, "user", MEMBER, DAY, "after")
        return "before snapshot"

    monkeypatch.setattr(reporting, "_generate_llm_report_content", model)
    saved = await generation.generate_company_report(TENANT, kind, DAY)
    assert saved.needs_refresh is True
    assert store.members[0].updated_at != before
    assert before is not None
    assert store.members[0].updated_at is not None
    assert store.members[0].updated_at < before


async def test_fallback_success_runs_without_connection(store: ReportStore, monkeypatch: pytest.MonkeyPatch) -> None:
    primary = LLMModelRecord(id=uuid4(), provider="openai", model="primary", api_key_encrypted="", label="primary")
    fallback = replace(primary, id=uuid4(), model="fallback")
    monkeypatch.setattr(
        reporting,
        "_resolve_report_models",
        AsyncMock(
            return_value=reporting.ResolvedReportModels(primary, fallback, None),
        ),
    )
    called: list[str] = []

    async def complete(**kwargs: object) -> dict[str, object]:
        assert get_connection() is None
        called.append(str(kwargs["model"]))
        if kwargs["model"] == "primary":
            raise TimeoutError("primary timeout")
        return {"choices": [{"message": {"content": "fallback success"}}]}

    monkeypatch.setattr(reporting, "chat_complete", complete)
    saved = await generation.generate_company_report(TENANT, "daily", DAY)
    assert called == ["primary", "fallback"]
    assert "fallback success" in saved.content
    assert saved.needs_refresh is False


@pytest.mark.parametrize("kind", ["weekly", "monthly"])
async def test_rollup_stays_dirty_until_missing_intermediate_is_generated(kind: str, store: ReportStore) -> None:
    await reporting.upsert_member_daily_report(TENANT, "user", MEMBER, DAY, "submitted progress")
    first = await generation.generate_company_report(TENANT, kind, DAY)
    second = await generation.generate_company_report(TENANT, kind, DAY)
    assert first.needs_refresh is True
    assert second.needs_refresh is True
    await generation.generate_company_report(TENANT, "daily", DAY)
    if kind == "monthly":
        await generation.generate_company_report(TENANT, "weekly", DAY)
    complete = await generation.generate_company_report(TENANT, kind, DAY)
    assert complete.needs_refresh is False
