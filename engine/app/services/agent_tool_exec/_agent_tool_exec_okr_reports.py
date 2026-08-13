from __future__ import annotations

import uuid
from datetime import date as date_cls

from app.core.logging import logger
from app.dao.agent_dao import agent_dao
from app.dao.okr_dao import member_daily_report_dao

from .registry import ToolArguments


async def _collect_okr_progress(agent_id: uuid.UUID | None) -> str:
    if not agent_id:
        return "OKR tools require agent context."
    try:
        from app.services.okr_scheduler import collect_all_focus_updates

        agent = await agent_dao.get(agent_id)
        if not agent:
            return "Agent not found."

        return await collect_all_focus_updates(
            tenant_id=agent.tenant_id,
            okr_agent_id=agent_id,
        )

    except Exception as error:
        logger.exception(f"[OKR] collect_okr_progress failed for agent {agent_id}")
        return f"Failed to collect OKR progress: {str(error)[:200]}"


async def _generate_okr_report(agent_id: uuid.UUID | None, arguments: ToolArguments) -> str:
    if not agent_id:
        return "OKR tools require agent context."

    report_type_value = arguments.get("report_type")
    report_type = report_type_value.lower() if isinstance(report_type_value, str) else "daily"
    if report_type not in ("daily", "weekly"):
        return "Invalid report_type. Must be 'daily' or 'weekly'."
    try:
        from app.services.okr_scheduler import generate_daily_report, generate_weekly_report

        agent = await agent_dao.get(agent_id)
        if not agent:
            return "Agent not found."

        if report_type == "daily":
            return await generate_daily_report(
                tenant_id=agent.tenant_id,
                okr_agent_id=agent_id,
            )
        return await generate_weekly_report(
            tenant_id=agent.tenant_id,
            okr_agent_id=agent_id,
        )

    except Exception as error:
        logger.exception(f"[OKR] generate_okr_report failed for agent {agent_id}")
        return f"Failed to generate OKR report: {str(error)[:200]}"


async def _generate_monthly_okr_report(agent_id: uuid.UUID | None) -> str:
    if not agent_id:
        return "OKR tools require agent context."
    try:
        from app.services.okr_scheduler import generate_monthly_report

        agent = await agent_dao.get(agent_id)
        if not agent:
            return "Agent not found."

        return await generate_monthly_report(
            tenant_id=agent.tenant_id,
            okr_agent_id=agent_id,
        )

    except Exception as error:
        logger.exception(f"[OKR] generate_monthly_okr_report failed for agent {agent_id}")
        return f"Failed to generate monthly OKR report: {str(error)[:200]}"


async def _upsert_member_daily_report(agent_id: uuid.UUID | None, arguments: ToolArguments) -> str:
    if not agent_id:
        return "OKR tools require agent context."
    try:
        from app.services.okr_reporting import (
            list_tracked_okr_members,
            upsert_member_daily_report as _upsert,
        )

        report_date_value = arguments.get("report_date")
        report_date_raw = report_date_value if isinstance(report_date_value, str) else ""
        content_value = arguments.get("content")
        content = content_value.strip() if isinstance(content_value, str) else ""
        member_type_value = arguments.get("member_type")
        member_type = member_type_value if isinstance(member_type_value, str) else "user"
        member_id_value = arguments.get("member_id")
        member_id_raw = member_id_value if isinstance(member_id_value, str) else ""
        member_name_value = arguments.get("member_name")
        member_name = member_name_value.strip() if isinstance(member_name_value, str) else ""
        source_value = arguments.get("source")
        source = source_value.strip() if isinstance(source_value, str) else "okr_agent_assisted"
        source = source or "okr_agent_assisted"

        if not report_date_raw or not content:
            return "Missing report_date or content"

        try:
            report_date = date_cls.fromisoformat(report_date_raw)
        except ValueError:
            return "Invalid report_date format. Use YYYY-MM-DD."

        ag = await agent_dao.get(agent_id)
        if not ag:
            return "Agent not found."
        if not ag.is_system:
            return "Permission denied: only the OKR Agent can upsert member daily reports."

        target_member_id: uuid.UUID | None = None
        if member_id_raw:
            try:
                target_member_id = uuid.UUID(member_id_raw)
            except ValueError:
                return "Invalid member_id format. Use a UUID."

        if not target_member_id:
            if not member_name:
                return "Provide either member_id or member_name."
            members = await list_tracked_okr_members(ag.tenant_id)
            lowered = member_name.casefold()
            exact_matches = [
                member
                for member in members
                if member.member_type == member_type and member.display_name.casefold() == lowered
            ]
            if len(exact_matches) == 1:
                target_member_id = exact_matches[0].member_id
                member_name = exact_matches[0].display_name
            elif len(exact_matches) > 1:
                return f"Multiple {member_type} members matched '{member_name}'. Please provide member_id."
            else:
                fuzzy_matches = [
                    member
                    for member in members
                    if member.member_type == member_type and lowered in member.display_name.casefold()
                ]
                if len(fuzzy_matches) == 1:
                    target_member_id = fuzzy_matches[0].member_id
                    member_name = fuzzy_matches[0].display_name
                elif len(fuzzy_matches) > 1:
                    options = ", ".join(member.display_name for member in fuzzy_matches[:5])
                    return (
                        f"Multiple {member_type} members matched '{member_name}': {options}. Please provide member_id."
                    )
                else:
                    return f"No {member_type} member matched '{member_name}'."

        existing = await member_daily_report_dao.get_for_member_date(
            ag.tenant_id,
            member_type=member_type,
            member_id=target_member_id,
            report_date=report_date,
        )
        previous_content = existing.content if existing else ""

        report = await _upsert(
            tenant_id=ag.tenant_id,
            member_type=member_type,
            member_id=target_member_id,
            report_date=report_date,
            content=content,
            source=source,
        )

        resolved_name = member_name or str(target_member_id)
        action = "Updated" if previous_content else "Created"
        details = [
            f"{action} daily report for {resolved_name} on {report.report_date.isoformat()}.",
            f"Stored length: {len(report.content)} characters.",
            f"Status: {report.status}.",
        ]
        if previous_content:
            details.append(f"Previous content: {previous_content}")
        details.append(f"Current content: {report.content}")
        return " ".join(details)
    except Exception as error:
        logger.exception("[OKR] upsert_member_daily_report failed")
        return f"Failed to upsert member daily report: {str(error)[:200]}"
