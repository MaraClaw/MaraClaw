from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import UUID, uuid4

from app.core.json_types import JsonObject
from app.dao.okr_dao import company_report_dao, member_daily_report_dao
from app.db.session import connection_ctx
from app.records.okr import CompanyReportRecord
from app.services import okr_reporting as reporting


@dataclass(frozen=True, slots=True)
class ReportInputs:
    content: str
    payload: JsonObject
    submitted_count: int
    missing_count: int
    members: tuple[tuple[UUID, datetime | None, date], ...]
    sources: tuple[tuple[UUID, datetime | None, bool], ...]
    roster: tuple[tuple[str, UUID, str], ...]
    unrepresented: bool

    @property
    def dirty(self) -> bool:
        return self.unrepresented or any(flagged for _, _, flagged in self.sources)


async def _read_inputs(tenant_id: UUID, report_type: str, start: date, end: date) -> ReportInputs:
    watermark_end = end + timedelta(days=6) if report_type == "monthly" else end
    members = tuple(await member_daily_report_dao.source_watermark(tenant_id, start, watermark_end))
    payload: JsonObject = {
        "report_type": report_type,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
    }
    roster: tuple[tuple[str, UUID, str], ...] = ()
    sources: tuple[tuple[UUID, datetime | None, bool], ...] = ()
    unrepresented = False
    if report_type == "daily":
        tracked = await reporting.list_tracked_okr_members(tenant_id)
        roster = tuple((member.member_type, member.member_id, member.display_name) for member in tracked)
        rows = await member_daily_report_dao.list_for_date(tenant_id, start)
        lookup = {(row.member_type, row.member_id): row for row in rows}
        submitted: list[reporting.SubmittedDailyItem] = []
        missing: list[reporting.MissingDailyItem] = []
        for member in tracked:
            row = lookup.get((member.member_type, member.member_id))
            if row is None:
                missing.append({"display_name": member.display_name})
            else:
                submitted.append({"display_name": member.display_name, "content": row.content})
        submitted_count, missing_count = len(submitted), len(missing)
        content = reporting._build_company_daily_content(start, submitted_count, missing, submitted)
        payload.update(
            submitted_members=[item["display_name"] for item in submitted],
            missing_members=[item["display_name"] for item in missing],
            submitted_reports=[
                {"member_name": item["display_name"], "content": reporting._truncate_for_prompt(item["content"])}
                for item in submitted
            ],
        )
    else:
        reader = (
            company_report_dao.list_dailies_in_range
            if report_type == "weekly"
            else company_report_dao.list_weeklies_in_range
        )
        reports = await reader(tenant_id, period_start=start, period_end=end)
        required_days = {day for _, _, day in members}
        descendants: list[CompanyReportRecord] = []
        if report_type == "monthly":
            descendants = list(
                await company_report_dao.list_dailies_in_range(
                    tenant_id,
                    period_start=start,
                    period_end=watermark_end,
                )
            )
            required_days.update(row.period_start for row in descendants)
            required_days = {day for day in required_days if start <= reporting._monday_of(day) <= end}
        unrepresented = any(
            not any(row.period_start <= day <= row.period_end for row in reports) for day in required_days
        )
        sources = tuple(sorted((row.id, row.updated_at, row.needs_refresh) for row in [*reports, *descendants]))
        submitted_count = max((row.submitted_count for row in reports), default=0)
        missing_count = max((row.missing_count for row in reports), default=0)
        content = reporting._build_company_rollup_content(
            reporting._default_report_headings(report_type)[0],
            start,
            end,
            reports,
            submitted_count=submitted_count,
            missing_count=missing_count,
        )
        payload.update(
            source_report_count=len(reports),
            source_reports=[
                {
                    "period_label": row.period_label,
                    "period_start": row.period_start.isoformat(),
                    "period_end": row.period_end.isoformat(),
                    "submitted_count": row.submitted_count,
                    "missing_count": row.missing_count,
                    "content": reporting._truncate_for_prompt(row.content, limit=1800),
                }
                for row in reports
            ],
        )
    payload.update(submitted_count=submitted_count, missing_count=missing_count)
    return ReportInputs(content, payload, submitted_count, missing_count, members, sources, roster, unrepresented)


async def generate_company_report(tenant_id: UUID, report_type: str, anchor: date) -> CompanyReportRecord:
    start = reporting._month_start(anchor) if report_type == "monthly" else anchor
    end = (
        reporting._month_end(anchor)
        if report_type == "monthly"
        else start + timedelta(days=6)
        if report_type == "weekly"
        else start
    )
    async with connection_ctx():
        await company_report_dao.lock_reporting(tenant_id)
        existing = await company_report_dao.get_for_period(
            tenant_id,
            report_type=report_type,
            period_start=start,
            period_end=end,
        )
        expected_updated_at = existing.updated_at if existing else None
        snapshot = await _read_inputs(tenant_id, report_type, start, end)
        models = await reporting._resolve_report_models(tenant_id)

    content = await reporting._generate_llm_report_content(
        tenant_id,
        report_type,
        start,
        end,
        snapshot.payload,
        fallback_content=snapshot.content,
        models=models,
    )

    async with connection_ctx():
        await company_report_dao.lock_reporting(tenant_id)
        current = await _read_inputs(tenant_id, report_type, start, end)
        stale = (
            snapshot.members != current.members
            or snapshot.sources != current.sources
            or snapshot.roster != current.roster
            or snapshot.dirty
            or current.dirty
        )
        draft = CompanyReportRecord(
            id=existing.id if existing else uuid4(),
            tenant_id=tenant_id,
            report_type=report_type,
            period_start=start,
            period_end=end,
            period_label=reporting._period_label(report_type, start, end),
            content=content,
            submitted_count=snapshot.submitted_count,
            missing_count=snapshot.missing_count,
            needs_refresh=stale,
        )
        return await company_report_dao.publish(
            draft,
            expected_updated_at=expected_updated_at,
        )
