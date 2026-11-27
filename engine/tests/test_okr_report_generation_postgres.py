import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import anyio
import pytest
from tests.llm_retry_fakes import health_model, install_provider_health, trip
from tests.test_openclaw_update_postgres import agent, db, member, tenant

from app.dao.okr_dao import company_report_dao, member_daily_report_dao
from app.dao.trigger_dao import agent_trigger_dao
from app.db.connection import DbConnection
from app.db.session import connection_ctx, get_connection
from app.records.okr import CompanyReportRecord
from app.records.trigger import AgentTriggerRecord
from app.services import okr_report_generation as generation, okr_reporting as reporting
from app.services.trigger_runtime import dispatch, executions, invoker
from app.services.trigger_runtime.evaluator import evaluate_trigger

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


async def test_open_provider_circuit_publishes_the_deterministic_report_without_a_request(
    reporting_tenant: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a report model whose provider circuit is open.
    tracker, _clock = install_provider_health(monkeypatch, threshold=2)
    primary = health_model("primary")
    trip(tracker, primary, failures=2)
    monkeypatch.setattr(
        reporting, "_resolve_report_models", AsyncMock(return_value=reporting.ResolvedReportModels(primary, None, None))
    )
    chat = AsyncMock()
    monkeypatch.setattr(reporting, "chat_complete", chat)

    # When the daily report is generated against the real database.
    saved = await generation.generate_company_report(reporting_tenant, "daily", DAY)

    # Then the deterministic report was published and no provider request went out.
    chat.assert_not_awaited()
    assert saved.content == reporting._build_company_daily_content(DAY, 0, [], [])
    assert [row.id for row in await company_report_dao.list_for_tenant(reporting_tenant)] == [saved.id]


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


class OkrFiring:
    """A due daily report trigger of an OKR-enabled tenant, and its generated reports counted per attempt."""

    def __init__(self, db: DbConnection, trigger: AgentTriggerRecord, tenant_id: UUID) -> None:
        self.db: DbConnection = db
        self.trigger: AgentTriggerRecord = trigger
        self.tenant_id: UUID = tenant_id
        self.attempts: int = 0
        self.failures: list[str] = []

    async def rows(self) -> list[dict[str, object]]:
        return await self.db.fetchall(
            "SELECT status, idempotency_key, payload, last_error, scheduled_at FROM trigger_executions "
            "WHERE trigger_id = %(id)s ORDER BY created_at",
            {"id": self.trigger.id},
        )

    async def claim_and_invoke(self) -> int:
        """Claim ready executions like a worker's tick and run their invocations; returns how many were claimed."""
        grouped, _forced = await dispatch.claim_ready_trigger_invocations(datetime.now(UTC), max_agents=5)
        for agent_id, triggers in grouped.items():
            await invoker.invoke_agent_for_triggers(agent_id, triggers)
        return sum(len(triggers) for triggers in grouped.values())

    async def reports(self) -> list[CompanyReportRecord]:
        return list(await company_report_dao.list_for_tenant(self.tenant_id))


@pytest.fixture
async def okr_firing(db: DbConnection, reporting_tenant: UUID, monkeypatch: pytest.MonkeyPatch) -> OkrFiring:
    creator = await member(db, reporting_tenant)
    okr_agent = await agent(db, reporting_tenant, creator)
    await db.execute(
        "INSERT INTO okr_settings (tenant_id, enabled, daily_report_enabled, okr_agent_id) "
        "VALUES (%(tenant)s, TRUE, TRUE, %(agent)s)",
        {"tenant": reporting_tenant, "agent": okr_agent},
    )
    trigger = await agent_trigger_dao.create(
        obj_in={
            "agent_id": okr_agent,
            "name": "daily_okr_report",
            "type": "cron",
            "config": {"expr": "0 9 * * *"},
            "is_system": True,
            "cooldown_seconds": 3600,
        }
    )
    firing = OkrFiring(db, trigger, reporting_tenant)
    real_generate = reporting.generate_company_daily_report

    async def generate(tenant_id: UUID, period_day: date) -> CompanyReportRecord:
        firing.attempts += 1
        if firing.failures:
            raise RuntimeError(firing.failures.pop(0))
        return await real_generate(tenant_id, period_day)

    monkeypatch.setattr(reporting, "generate_company_daily_report", generate)
    return firing


async def test_racing_workers_and_repeated_ticks_send_and_generate_once(okr_firing: OkrFiring) -> None:
    # Given a due report trigger that four workers and two later ticks all try to enqueue
    now = datetime.now(UTC)
    await asyncio.gather(*(dispatch.enqueue_due_trigger(okr_firing.trigger, now, "UTC") for _ in range(4)))
    # When one worker claims and runs it, and the trigger is enqueued again afterwards
    assert await okr_firing.claim_and_invoke() == 1
    await dispatch.enqueue_due_trigger(okr_firing.trigger, now + timedelta(seconds=15), "UTC")
    await dispatch.enqueue_due_trigger(okr_firing.trigger, now + timedelta(seconds=30), "UTC")
    assert await okr_firing.claim_and_invoke() == 0
    # Then one execution keyed by the trigger and its period ran once: one generation, one report, completed
    [row] = await okr_firing.rows()
    period = (now.astimezone(UTC).date() - timedelta(days=1)).isoformat()
    assert row["status"] == "completed"
    assert row["idempotency_key"] == f"okr:{okr_firing.trigger.id}:{period}"
    assert row["payload"] == {"okr_period": period}
    assert okr_firing.attempts == 1
    assert [report.period_start.isoformat() for report in await okr_firing.reports()] == [period]


async def test_failed_generation_is_recorded_and_retried_without_refiring_every_tick(okr_firing: OkrFiring) -> None:
    # Given a provider outage on the first attempt
    okr_firing.failures = ["provider down"]
    now = datetime.now(UTC)
    await dispatch.enqueue_due_trigger(okr_firing.trigger, now, "UTC")
    # When the first attempt runs and the daemon ticks again every 15 seconds
    assert await okr_firing.claim_and_invoke() == 1
    stored = await agent_trigger_dao.get(okr_firing.trigger.id)
    assert stored is not None
    for tick in range(1, 4):
        later = now + timedelta(seconds=15 * tick)
        assert await evaluate_trigger(stored, later, agent_timezone="UTC") is False
        assert await okr_firing.claim_and_invoke() == 0
    # Then the failure is on the execution, which waits queued for its retry time instead of being refired
    [row] = await okr_firing.rows()
    assert (row["status"], row["last_error"]) == ("pending", "provider down")
    assert isinstance(row["scheduled_at"], datetime)
    assert row["scheduled_at"] > datetime.now(UTC) + executions.EXECUTION_RETRY_DELAY - timedelta(minutes=1)
    assert okr_firing.attempts == 1
    assert await okr_firing.reports() == []
    # And once the retry time has passed, the same execution runs again and completes with one report
    await okr_firing.db.execute("UPDATE trigger_executions SET scheduled_at = %(at)s", {"at": now})
    assert await okr_firing.claim_and_invoke() == 1
    [row] = await okr_firing.rows()
    assert row["status"] == "completed"
    assert okr_firing.attempts == 2
    assert len(await okr_firing.reports()) == 1
    stored = await agent_trigger_dao.get(okr_firing.trigger.id)
    assert stored is not None
    assert stored.fire_count == 1


async def test_tenant_without_okr_completes_the_execution_without_generating(okr_firing: OkrFiring) -> None:
    # Given a tenant that switched OKR off after the trigger was queued
    await okr_firing.db.execute(
        "UPDATE okr_settings SET enabled = FALSE WHERE tenant_id = %(id)s", {"id": okr_firing.tenant_id}
    )
    await dispatch.enqueue_due_trigger(okr_firing.trigger, datetime.now(UTC), "UTC")
    # When it runs
    assert await okr_firing.claim_and_invoke() == 1
    # Then nothing was generated and the execution still completed
    [row] = await okr_firing.rows()
    assert row["status"] == "completed"
    assert okr_firing.attempts == 0
