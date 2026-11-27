"""Deterministic OKR firings: the report period an execution is keyed by, and the work it runs.

The system OKR triggers send the daily collection and generate the company reports without a model. They go through
``trigger_executions`` like every other trigger: the daemon enqueues one execution per trigger and report period, and
the invoker runs it under the execution lease.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Final

from app.core.json_types import json_as_str, json_object_from
from app.core.logging import logger
from app.dao.agent_dao import agent_dao
from app.db.session import connection_ctx
from app.records.trigger import AgentTriggerRecord
from app.services.okr_daily_collection import local_day, trigger_daily_collection_for_tenant
from app.services.trigger_runtime.evaluator import schedule_timezone

OKR_COLLECTION_TRIGGER: Final = "daily_okr_collection"
OKR_REPORT_TRIGGERS: Final = frozenset({"daily_okr_report", "weekly_okr_report", "monthly_okr_report"})
OKR_PERIOD_KEY: Final = "okr_period"
"""Execution payload key holding the ISO report period the execution is keyed by and runs for."""


def is_okr_firing(trigger: AgentTriggerRecord) -> bool:
    return trigger.name == OKR_COLLECTION_TRIGGER or trigger.name in OKR_REPORT_TRIGGERS


def report_period(trigger_name: str, local_today: date) -> date:
    """The period a firing on ``local_today`` is for: the day to collect, or the generation anchor of a report."""
    if trigger_name == "daily_okr_report":
        return local_today - timedelta(days=1)
    if trigger_name == "weekly_okr_report":
        previous_week_anchor = local_today - timedelta(days=7)
        return previous_week_anchor - timedelta(days=previous_week_anchor.weekday())
    if trigger_name == "monthly_okr_report":
        return local_today.replace(day=1) - timedelta(days=1)
    return local_today


async def due_period(trigger: AgentTriggerRecord, now: datetime, agent_timezone: str | None) -> date:
    """The period of a firing due at ``now``, with the local day taken in the zone the schedule is evaluated in."""
    zone = await schedule_timezone(trigger, agent_timezone)
    return report_period(trigger.name, local_day(now, zone))


async def run_okr_firing(trigger: AgentTriggerRecord, *, guard: Callable[[], None]) -> None:
    """Send or generate for the period stored on the execution; ``guard`` raises once the lease is lost.

    A tenant without OKR enabled has nothing to send, which completes the execution.
    """
    from app.services.okr_reporting import (
        generate_company_daily_report,
        generate_company_monthly_report,
        generate_company_weekly_report,
    )

    period_text = json_as_str(json_object_from(trigger.config).get(OKR_PERIOD_KEY))
    if period_text in (None, ""):
        raise ValueError(f"OKR execution of trigger {trigger.name} carries no report period")
    period = date.fromisoformat(period_text)

    agent = await agent_dao.get(trigger.agent_id)
    if not agent or not agent.tenant_id:
        return
    tenant_id = agent.tenant_id
    collecting = trigger.name == OKR_COLLECTION_TRIGGER
    async with connection_ctx() as db:
        settings = await db.fetchone(
            "SELECT enabled, daily_report_enabled FROM okr_settings WHERE tenant_id = %(tenant_id)s",
            {"tenant_id": tenant_id},
        )
    if settings is None or not settings.get("enabled") or (collecting and not settings.get("daily_report_enabled")):
        return

    guard()
    if collecting:
        _ = await trigger_daily_collection_for_tenant(tenant_id, period, guard=guard)
    elif trigger.name == "daily_okr_report":
        _ = await generate_company_daily_report(tenant_id, period)
    elif trigger.name == "weekly_okr_report":
        _ = await generate_company_weekly_report(tenant_id, period)
    else:
        _ = await generate_company_monthly_report(tenant_id, period)
    logger.info(f"[Trigger] OKR firing {trigger.name} done for {period}")
