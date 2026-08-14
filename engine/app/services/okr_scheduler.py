"""OKR Scheduler - batch progress collection and report generation.

Provides functions called by OKR Agent tools:
  - collect_all_focus_updates(): read all Agent focus.md files and sync progress
  - generate_daily_report():     build and store a daily OKR report
  - generate_weekly_report():    build and store a weekly OKR report

Design decisions:
  - Direct DB writes (no HTTP round-trips) for efficiency
  - focus.md is parsed with regex, not LLM, to avoid token cost for simple extraction
  - Reports are stored in WorkReport table AND returned as strings to the caller
    so the OKR Agent LLM can post to plaza / send to channels as it sees fit
  - All errors are caught per-agent so one bad focus.md doesn't block the batch
"""

import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Required, TypedDict

from app.core.logging import logger
from app.dao.agent_dao import agent_dao
from app.dao.okr_dao import okr_key_result_dao, okr_objective_dao, okr_progress_log_dao, work_report_dao
from app.dao.okr_settings_dao import okr_settings_dao
from app.records.okr import OKRKeyResultRecord, OKRObjectiveRecord
from app.services.storage import agent_storage_key, get_storage_backend, store_agent_bytes

type KRsByObjective = dict[str, list[OKRKeyResultRecord]]


class OKRAgentSettings(TypedDict, total=False):
    enabled: Required[bool]
    daily_report_enabled: bool
    daily_report_time: str | None
    daily_report_skip_non_workdays: bool
    weekly_report_enabled: bool
    weekly_report_day: int
    period_frequency: str
    period_length_days: int | None


# ─── Focus File Parsing ───────────────────────────────────────────────────────

_KR_ID_RE = re.compile(
    r"\*\*KR ID\*\*[:\s]+([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)

_PROGRESS_RE = re.compile(
    r"\*\*(?:Current Progress|当前进度)\*\*[:\s]+([\d.]+)",
    re.IGNORECASE,
)

_NOTE_RE = re.compile(
    r"\*\*(?:This Week|本期工作)\*\*[:\s]+(.+)",
    re.IGNORECASE,
)


def _parse_focus_md(content: str) -> list[tuple[str, float, str]]:
    """Parse a focus.md file and extract KR updates."""
    results: list[tuple[str, float, str]] = []
    sections = re.split(r"(?m)^##\s+KR:", content)

    for section in sections[1:]:
        kr_id_match = _KR_ID_RE.search(section)
        progress_match = _PROGRESS_RE.search(section)

        if not kr_id_match or not progress_match:
            continue

        kr_id_str = kr_id_match.group(1).lower()
        try:
            value = float(progress_match.group(1))
        except ValueError:
            continue

        note_match = _NOTE_RE.search(section)
        note = note_match.group(1).strip() if note_match else ""
        results.append((kr_id_str, value, note))

    return results


# ─── Progress Collection ───────────────────────────────────────────────────────


async def collect_all_focus_updates(
    tenant_id: uuid.UUID,
    okr_agent_id: uuid.UUID,
) -> str:
    """Read every Agent's focus.md and sync KR progress to the database."""
    agents = await agent_dao.list_for_tenant(tenant_id)
    agents = [a for a in agents if a.id != okr_agent_id]

    if not agents:
        return "No team members found. No focus files to collect."

    updated_count = 0
    skipped_count = 0
    error_count = 0
    lines: list[str] = []
    storage = get_storage_backend()

    for agent in agents:
        focus_key = agent_storage_key(agent.id, "focus.md")
        if not await storage.exists(focus_key):
            skipped_count += 1
            continue

        try:
            content = await storage.read_text(focus_key, encoding="utf-8", errors="replace")
            updates = _parse_focus_md(content)

            if not updates:
                skipped_count += 1
                continue

            for kr_id_str, value, note in updates:
                try:
                    kr_uuid = uuid.UUID(kr_id_str)
                except ValueError:
                    logger.warning(f"[OKRScheduler] Invalid KR UUID '{kr_id_str}' in {focus_key}")
                    continue

                row = await okr_key_result_dao.get_with_tenant(kr_uuid, tenant_id)
                if not row:
                    logger.warning(f"[OKRScheduler] KR {kr_uuid} not found or wrong tenant, skipping")
                    continue

                kr, _ = row

                if abs(kr.current_value - value) < 0.001:
                    continue

                prev_value = kr.current_value
                status = kr.status
                if kr.target_value:
                    ratio = value / kr.target_value
                    if ratio >= 1.0:
                        status = "completed"
                    elif ratio >= 0.7:
                        status = "on_track"
                    elif ratio >= 0.4:
                        status = "at_risk"
                    else:
                        status = "behind"

                _ = await okr_key_result_dao.update(
                    db_obj=kr,
                    obj_in={
                        "current_value": value,
                        "status": status,
                        "last_updated_at": datetime.now(UTC),
                    },
                )
                _ = await okr_progress_log_dao.create(
                    obj_in={
                        "kr_id": kr_uuid,
                        "previous_value": prev_value,
                        "new_value": value,
                        "source": "okr_agent",
                        "note": f"[focus.md] {note}" if note else "[focus.md] Auto-collected",
                    }
                )
                updated_count += 1
                lines.append(f"  - {agent.name} / {kr.title}: {prev_value} → {value} ({status})")

        except Exception:
            logger.exception(f"[OKRScheduler] Failed to process focus.md for agent {agent.id}")
            error_count += 1

    summary = (
        f"Focus file collection complete.\n"
        + f"  KRs updated: {updated_count}\n"
        + f"  Agents without focus.md: {skipped_count}\n"
        + f"  Errors: {error_count}\n"
    )
    if lines:
        summary += "\nChanges:\n" + "\n".join(lines)

    return summary


# ─── Report Generation ────────────────────────────────────────────────────────


def _compute_period(
    frequency: str,
    length_days: int | None,
    target_date: date | None = None,
) -> tuple[date, date]:
    """Compute OKR period start/end dates for a target date. Mirrors okr.py logic."""
    today = target_date or datetime.now(UTC).date()
    if frequency == "monthly":
        start = today.replace(day=1)
        if today.month == 12:
            end = today.replace(month=12, day=31)
        else:
            end = today.replace(month=today.month + 1, day=1) - timedelta(days=1)
    elif frequency == "custom" and length_days:
        epoch = date(1970, 1, 1)
        days_since_epoch = (today - epoch).days
        period_index = days_since_epoch // length_days
        start = epoch + timedelta(days=period_index * length_days)
        end = start + timedelta(days=length_days - 1)
    else:
        quarter = (today.month - 1) // 3 + 1
        start = date(today.year, (quarter - 1) * 3 + 1, 1)
        end = (date(today.year, quarter * 3 + 1, 1) - timedelta(days=1)) if quarter < 4 else date(today.year, 12, 31)
    return start, end


async def _build_okr_snapshot(
    tenant_id: uuid.UUID,
    frequency: str,
    length_days: int | None,
    target_date: date | None = None,
) -> tuple[Sequence[OKRObjectiveRecord], KRsByObjective, date, date]:
    """Fetch period objectives and KRs for report building."""
    ps, pe = _compute_period(frequency, length_days, target_date)
    objectives = await okr_objective_dao.list_for_period(tenant_id, period_start=ps, period_end=pe)
    krs_by_obj: KRsByObjective = {}
    if objectives:
        all_krs = await okr_key_result_dao.list_for_objectives([o.id for o in objectives])
        for kr in all_krs:
            krs_by_obj.setdefault(str(kr.objective_id), []).append(kr)
    return objectives, krs_by_obj, ps, pe


def _format_report_body(
    objectives: Sequence[OKRObjectiveRecord],
    krs_by_obj: Mapping[str, Sequence[OKRKeyResultRecord]],
    period_start: date,
    period_end: date,
    report_type: str,
) -> str:
    """Build a structured Markdown report from OKR data."""
    today = datetime.now(UTC).date()
    header = (
        f"# OKR {'Daily' if report_type == 'daily' else 'Weekly'} Report\n"
        + f"**Date**: {today.isoformat()}  |  "
        + f"**Period**: {period_start.isoformat()} - {period_end.isoformat()}\n\n"
    )

    if not objectives:
        return header + "_No active OKRs found for this period._\n"

    all_krs: list[OKRKeyResultRecord] = []
    for krs in krs_by_obj.values():
        all_krs.extend(krs)

    status_counts: dict[str, int] = {}
    for kr in all_krs:
        status_counts[kr.status] = status_counts.get(kr.status, 0) + 1

    total_krs = len(all_krs)
    on_track = status_counts.get("on_track", 0) + status_counts.get("completed", 0)
    at_risk = status_counts.get("at_risk", 0)
    behind = status_counts.get("behind", 0)

    lines = [header]
    lines.append("## Health Summary\n")
    lines.append("| Status | Count | % |\n|---|---|---|")
    if total_krs:
        lines.append(f"| On Track / Completed | {on_track} | {on_track * 100 // total_krs}% |")
        lines.append(f"| At Risk | {at_risk} | {at_risk * 100 // total_krs}% |")
        lines.append(f"| Behind | {behind} | {behind * 100 // total_krs}% |")
    lines.append("")

    attention_krs = [kr for kr in all_krs if kr.status in ("at_risk", "behind")]
    if attention_krs:
        lines.append("## Needs Attention\n")
        for kr in attention_krs:
            pct = int(kr.current_value / kr.target_value * 100) if kr.target_value else 0
            lines.append(
                f"- **[{kr.status.upper()}]** {kr.title} - {pct}% ({kr.current_value}/{kr.target_value} {kr.unit or ''})"
            )
        lines.append("")

    company_objs = [o for o in objectives if o.owner_type == "company"]
    if company_objs:
        lines.append("## Company Objectives\n")
        for o in company_objs:
            krs = krs_by_obj.get(str(o.id), [])
            pct = 0
            if krs:
                pct = int(sum(min(k.current_value / k.target_value, 1) for k in krs if k.target_value) / len(krs) * 100)
            lines.append(f"### {o.title} [{pct}%]\n")
            for kr in krs:
                kr_pct = int(kr.current_value / kr.target_value * 100) if kr.target_value else 0
                bar = "█" * (kr_pct // 10) + "░" * (10 - kr_pct // 10)
                lines.append(f"- {bar} {kr.title}")
                lines.append(f"  {kr.current_value}/{kr.target_value} {kr.unit or ''} ({kr_pct}%) - _{kr.status}_")
            lines.append("")

    member_objs = [o for o in objectives if o.owner_type != "company"]
    if member_objs:
        lines.append("## Member Objectives\n")
        for o in member_objs:
            krs = krs_by_obj.get(str(o.id), [])
            lines.append(f"### {o.owner_type}:{o.owner_id} - {o.title}\n")
            for kr in krs:
                kr_pct = int(kr.current_value / kr.target_value * 100) if kr.target_value else 0
                lines.append(
                    f"- {kr.title}: {kr.current_value}/{kr.target_value} {kr.unit or ''} ({kr_pct}%) - _{kr.status}_"
                )
            lines.append("")

    return "\n".join(lines)


async def _store_report(
    tenant_id: uuid.UUID,
    okr_agent_id: uuid.UUID,
    report_type: str,
    period_date: date,
    content: str,
) -> None:
    """Write a report to the WorkReport table."""
    _ = await work_report_dao.create(
        obj_in={
            "tenant_id": tenant_id,
            "author_type": "agent",
            "author_id": okr_agent_id,
            "report_type": report_type,
            "period_date": period_date,
            "content": content,
            "source": "okr_agent_collected",
        }
    )


async def _safe_write_report(okr_agent_id: uuid.UUID, filename: str, content: str) -> None:
    """Write report to OKR Agent's workspace/reports/ directory."""
    try:
        _ = await store_agent_bytes(
            okr_agent_id,
            f"workspace/reports/{filename}",
            content.encode("utf-8"),
            content_type="text/markdown; charset=utf-8",
        )
    except Exception as exc:
        logger.warning(f"[OKRScheduler] Could not write report file {filename}: {exc}")


async def generate_daily_report(
    tenant_id: uuid.UUID,
    okr_agent_id: uuid.UUID,
) -> str:
    """Generate and store a daily OKR report."""
    okr_settings = await okr_settings_dao.get_by_tenant(tenant_id)
    if not okr_settings or not okr_settings.enabled:
        return "OKR is not enabled for this tenant."

    objectives, krs_by_obj, ps, pe = await _build_okr_snapshot(
        tenant_id, okr_settings.period_frequency, okr_settings.period_length_days
    )
    content = _format_report_body(objectives, krs_by_obj, ps, pe, "daily")
    today = datetime.now(UTC).date()
    await _store_report(tenant_id, okr_agent_id, "daily", today, content)
    await _safe_write_report(okr_agent_id, f"daily_{today.strftime('%Y%m%d')}.md", content)
    logger.info(f"[OKRScheduler] Daily report generated for tenant {tenant_id}")
    return content


async def generate_weekly_report(
    tenant_id: uuid.UUID,
    okr_agent_id: uuid.UUID,
) -> str:
    """Generate and store a weekly OKR report."""
    okr_settings = await okr_settings_dao.get_by_tenant(tenant_id)
    if not okr_settings or not okr_settings.enabled:
        return "OKR is not enabled for this tenant."

    previous_month_ref = datetime.now(UTC).date().replace(day=1) - timedelta(days=1)
    objectives, krs_by_obj, ps, pe = await _build_okr_snapshot(
        tenant_id,
        okr_settings.period_frequency,
        okr_settings.period_length_days,
        target_date=previous_month_ref,
    )
    content = _format_report_body(objectives, krs_by_obj, ps, pe, "weekly")
    today = datetime.now(UTC).date()
    monday = today - timedelta(days=today.weekday())
    await _store_report(tenant_id, okr_agent_id, "weekly", monday, content)
    week_label = monday.strftime("%Y-W%V")
    await _safe_write_report(okr_agent_id, f"weekly_{week_label}.md", content)
    logger.info(f"[OKRScheduler] Weekly report generated for tenant {tenant_id}")
    return content


async def get_okr_settings_for_agent(tenant_id: uuid.UUID) -> OKRAgentSettings:
    """Return OKR configuration for the tenant as a plain dict."""
    s = await okr_settings_dao.get_by_tenant(tenant_id)
    if not s:
        return {"enabled": False}
    return {
        "enabled": s.enabled,
        "daily_report_enabled": s.daily_report_enabled,
        "daily_report_time": s.daily_report_time,
        "daily_report_skip_non_workdays": s.daily_report_skip_non_workdays,
        "weekly_report_enabled": s.weekly_report_enabled,
        "weekly_report_day": s.weekly_report_day,
        "period_frequency": s.period_frequency,
        "period_length_days": s.period_length_days,
    }


async def generate_monthly_report(
    tenant_id: uuid.UUID,
    okr_agent_id: uuid.UUID,
) -> str:
    """Generate and store a monthly OKR progress report."""
    okr_settings = await okr_settings_dao.get_by_tenant(tenant_id)
    if not okr_settings or not okr_settings.enabled:
        return "OKR is not enabled for this tenant."

    objectives, krs_by_obj, ps, pe = await _build_okr_snapshot(
        tenant_id, okr_settings.period_frequency, okr_settings.period_length_days
    )
    content = _format_monthly_report_body(objectives, krs_by_obj, ps, pe)
    month_start = ps
    await _store_report(tenant_id, okr_agent_id, "monthly", month_start, content)
    month_label = month_start.strftime("%Y-%m")
    await _safe_write_report(okr_agent_id, f"monthly_{month_label}.md", content)
    logger.info(f"[OKRScheduler] Monthly report generated for tenant {tenant_id}")
    return content


def _format_monthly_report_body(
    objectives: Sequence[OKRObjectiveRecord],
    krs_by_obj: Mapping[str, Sequence[OKRKeyResultRecord]],
    period_start: date,
    period_end: date,
) -> str:
    """Build a monthly OKR report in structured Markdown."""
    today = datetime.now(UTC).date()
    month_label = period_start.strftime("%B %Y")
    header = (
        f"# Monthly OKR Report - {month_label}\n"
        + f"**Generated**: {today.isoformat()}  "
        + f"| **Period**: {period_start.isoformat()} - {period_end.isoformat()}\n\n"
    )

    if not objectives:
        return header + "_No active OKRs found for this period._\n"

    all_krs: list[OKRKeyResultRecord] = []
    for krs in krs_by_obj.values():
        all_krs.extend(krs)

    total_krs = len(all_krs)
    completed = sum(1 for kr in all_krs if kr.status == "completed")
    on_track = sum(1 for kr in all_krs if kr.status == "on_track")
    at_risk = sum(1 for kr in all_krs if kr.status == "at_risk")
    behind = sum(1 for kr in all_krs if kr.status == "behind")

    lines = [header]
    lines.append("## Monthly Health Summary\n")
    if total_krs:
        lines.append("| Status | Count | Ratio |")
        lines.append("|---|---|---|")
        lines.append(f"| Completed   | {completed} | {completed * 100 // total_krs}% |")
        lines.append(f"| On Track    | {on_track}  | {on_track * 100 // total_krs}% |")
        lines.append(f"| At Risk     | {at_risk}   | {at_risk * 100 // total_krs}% |")
        lines.append(f"| Behind      | {behind}    | {behind * 100 // total_krs}% |")
    else:
        lines.append("_No Key Results tracked this month._")
    lines.append("")

    company_objs = [o for o in objectives if o.owner_type == "company"]
    if company_objs:
        lines.append("## Company Objectives\n")
        for o in company_objs:
            krs = krs_by_obj.get(str(o.id), [])
            pct = 0
            if krs:
                pct = int(sum(min(k.current_value / k.target_value, 1) for k in krs if k.target_value) / len(krs) * 100)
            lines.append(f"### {o.title}  -  {pct}% overall\n")
            for kr in krs:
                kr_pct = int(kr.current_value / kr.target_value * 100) if kr.target_value else 0
                bar = "█" * (kr_pct // 10) + "░" * (10 - kr_pct // 10)
                status_badge = {
                    "completed": "DONE",
                    "on_track": "OK",
                    "at_risk": "RISK",
                    "behind": "BEHIND",
                }.get(kr.status, kr.status.upper())
                lines.append(f"- [{status_badge}] {bar} {kr.title}")
                lines.append(f"  {kr.current_value} / {kr.target_value} {kr.unit or ''} ({kr_pct}%)")
            lines.append("")

    member_objs = [o for o in objectives if o.owner_type != "company"]
    if member_objs:
        lines.append("## Member Objectives\n")
        for o in member_objs:
            krs = krs_by_obj.get(str(o.id), [])
            lines.append(f"### {o.owner_type}: {o.title}\n")
            for kr in krs:
                kr_pct = int(kr.current_value / kr.target_value * 100) if kr.target_value else 0
                lines.append(
                    f"- {kr.title}: {kr.current_value}/{kr.target_value} {kr.unit or ''} ({kr_pct}%) - _{kr.status}_"
                )
            lines.append("")

    attention_krs = [kr for kr in all_krs if kr.status in ("at_risk", "behind")]
    if attention_krs:
        lines.append("## Action Required\n")
        lines.append("The following Key Results need attention heading into next month:\n")
        for kr in attention_krs:
            kr_pct = int(kr.current_value / kr.target_value * 100) if kr.target_value else 0
            lines.append(f"- **{kr.status.upper()}** - {kr.title} ({kr_pct}%)")
        lines.append("")

    lines.append("---")
    lines.append(
        "_This report was auto-generated by the OKR Agent. "
        + "Please review the items needing attention and align with team members "
        + "before the next check-in._"
    )
    return "\n".join(lines)
