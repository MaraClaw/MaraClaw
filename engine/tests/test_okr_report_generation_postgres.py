from dataclasses import replace
from datetime import date, timedelta
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import anyio
import pytest
from tests.test_openclaw_update_postgres import db, tenant

from app.dao.okr_dao import company_report_dao, member_daily_report_dao
from app.db.connection import DbConnection
from app.db.session import connection_ctx, get_connection
from app.records.okr import CompanyReportRecord
from app.services import okr_report_generation as generation, okr_reporting as reporting

__all__ = ["db"]
DAY = date(2026, 11, 2)


@pytest.fixture
async def reporting_tenant(db: DbConnection, monkeypatch: pytest.MonkeyPatch) -> UUID:
    tenant_id = await tenant(db)
    monkeypatch.setattr(reporting, "list_tracked_okr_members", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        reporting,
        "_resolve_report_models",
        AsyncMock(
            return_value=reporting.ResolvedReportModels(None, None, None),
        ),
    )
    return tenant_id


@pytest.mark.parametrize("kind", ["daily", "weekly", "monthly"])
@pytest.mark.parametrize("exists", [False, True])
async def test_publish_waits_for_member_commit_even_without_company_row(
    kind: str,
    exists: bool,
    reporting_tenant: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = reporting_tenant
    if exists:
        await generation.generate_company_report(tenant_id, kind, DAY)
    model_entered, model_release = anyio.Event(), anyio.Event()
    member_staged, member_commit = anyio.Event(), anyio.Event()
    publishing = anyio.Event()
    saved: list[CompanyReportRecord] = []
    real_lock = company_report_dao.lock_reporting

    async def lock(locked_tenant: UUID) -> None:
        if member_staged.is_set():
            publishing.set()
        await real_lock(locked_tenant)

    async def model(*_args: object, **_kwargs: object) -> str:
        assert get_connection() is None
        model_entered.set()
        await model_release.wait()
        return "snapshot report"

    async def generate() -> None:
        saved.append(await generation.generate_company_report(tenant_id, kind, DAY))

    async def write_member() -> None:
        async with connection_ctx():
            await reporting.upsert_member_daily_report(tenant_id, "user", uuid4(), DAY, "late submission")
            member_staged.set()
            await member_commit.wait()

    monkeypatch.setattr(company_report_dao, "lock_reporting", lock)
    monkeypatch.setattr(reporting, "_generate_llm_report_content", model)
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(generate)
            await model_entered.wait()
            tasks.start_soon(write_member)
            await member_staged.wait()
            model_release.set()
            await publishing.wait()
            assert saved == []
            member_commit.set()
    assert saved[0].needs_refresh is True
    assert len(await company_report_dao.list_for_tenant(tenant_id)) == 1
    assert len(await member_daily_report_dao.list_for_date(tenant_id, DAY)) == 1


@pytest.mark.parametrize("exists", [False, True])
async def test_guarded_upsert_rejects_older_snapshot(reporting_tenant: UUID, exists: bool) -> None:
    draft = CompanyReportRecord(
        id=uuid4(),
        tenant_id=reporting_tenant,
        report_type="daily",
        period_start=DAY,
        period_end=DAY,
        content="initial",
    )
    initial = await company_report_dao.publish(draft, expected_updated_at=None) if exists else None
    expected = initial.updated_at if initial else None
    async with connection_ctx():
        await company_report_dao.lock_reporting(reporting_tenant)
        newer = await company_report_dao.publish(replace(draft, content="new snapshot"), expected_updated_at=expected)
    async with connection_ctx():
        await company_report_dao.lock_reporting(reporting_tenant)
        older = await company_report_dao.publish(replace(draft, content="old snapshot"), expected_updated_at=expected)
    assert older == newer
    assert older.content == "new snapshot"
    assert len(await company_report_dao.list_for_tenant(reporting_tenant)) == 1


@pytest.mark.parametrize("kind", ["weekly", "monthly"])
async def test_current_source_refresh_flag_survives_rollup(reporting_tenant: UUID, kind: str) -> None:
    source_kind = "daily" if kind == "weekly" else "weekly"
    source = CompanyReportRecord(
        id=uuid4(),
        tenant_id=reporting_tenant,
        report_type=source_kind,
        period_start=DAY,
        period_end=DAY,
        content="flagged source",
        needs_refresh=True,
    )
    await company_report_dao.publish(source, expected_updated_at=None)
    saved = await generation.generate_company_report(reporting_tenant, kind, DAY)
    assert saved.needs_refresh is True


async def test_member_submission_flags_month_containing_source_week_start(reporting_tenant: UUID) -> None:
    october = await generation.generate_company_report(reporting_tenant, "monthly", date(2026, 10, 1))
    assert october.needs_refresh is False
    await reporting.upsert_member_daily_report(reporting_tenant, "user", uuid4(), date(2026, 11, 1), "Sunday report")
    current = await company_report_dao.get_for_period(
        reporting_tenant,
        report_type="monthly",
        period_start=date(2026, 10, 1),
        period_end=date(2026, 10, 31),
    )
    assert current is not None
    assert current.needs_refresh is True


async def test_database_clock_watermark_detects_backdated_member_update(
    reporting_tenant: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = reporting_tenant
    member_id = uuid4()
    original = await reporting.upsert_member_daily_report(tenant_id, "user", member_id, DAY, "before")

    async def model(*_args: object, **_kwargs: object) -> str:
        async with connection_ctx() as connection:
            await company_report_dao.lock_reporting(tenant_id)
            await connection.execute(
                "UPDATE member_daily_reports SET content = 'after', updated_at = updated_at - %(delta)s WHERE id = %(id)s",
                {"id": original.id, "delta": timedelta(days=1)},
            )
        return "before snapshot"

    monkeypatch.setattr(reporting, "_generate_llm_report_content", model)
    saved = await generation.generate_company_report(tenant_id, "daily", DAY)
    assert saved.needs_refresh is True


async def test_source_publication_invalidates_existing_ancestors(reporting_tenant: UUID) -> None:
    tenant_id = reporting_tenant
    await generation.generate_company_report(tenant_id, "daily", DAY)
    weekly = await generation.generate_company_report(tenant_id, "weekly", DAY)
    monthly = await generation.generate_company_report(tenant_id, "monthly", DAY)
    assert weekly.needs_refresh is False
    assert monthly.needs_refresh is False
    await generation.generate_company_report(tenant_id, "daily", DAY)
    for row in await company_report_dao.list_for_tenant(tenant_id):
        if row.report_type in {"weekly", "monthly"}:
            assert row.needs_refresh is True
    await generation.generate_company_report(tenant_id, "weekly", DAY)
    monthly = await generation.generate_company_report(tenant_id, "monthly", DAY)
    assert monthly.needs_refresh is False
    await generation.generate_company_report(tenant_id, "weekly", DAY)
    current = await company_report_dao.get_for_period(
        tenant_id,
        report_type="monthly",
        period_start=monthly.period_start,
        period_end=monthly.period_end,
    )
    assert current is not None
    assert current.needs_refresh is True


async def test_rejected_publish_returns_competing_row_with_source_invalidation(
    reporting_tenant: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = reporting_tenant
    await generation.generate_company_report(tenant_id, "daily", DAY)
    entered, release = anyio.Event(), anyio.Event()
    old_result: list[CompanyReportRecord] = []
    calls = 0

    async def model(*_args: object, **_kwargs: object) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return "generated"

    async def old() -> None:
        old_result.append(await generation.generate_company_report(tenant_id, "weekly", DAY))

    monkeypatch.setattr(reporting, "_generate_llm_report_content", model)
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(old)
            await entered.wait()
            newer = await generation.generate_company_report(tenant_id, "weekly", DAY)
            assert newer.needs_refresh is False
            await generation.generate_company_report(tenant_id, "daily", DAY)
            release.set()
    assert old_result[0].needs_refresh is True


async def test_daily_publication_invalidates_month_without_weekly(reporting_tenant: UUID) -> None:
    monthly = await generation.generate_company_report(reporting_tenant, "monthly", DAY)
    assert monthly.needs_refresh is False
    await generation.generate_company_report(reporting_tenant, "daily", DAY)
    current = await company_report_dao.get_for_period(
        reporting_tenant,
        report_type="monthly",
        period_start=monthly.period_start,
        period_end=monthly.period_end,
    )
    assert current is not None
    assert current.needs_refresh is True
