from __future__ import annotations

import importlib
import json
import uuid
from datetime import date
from types import ModuleType

from app.core.logging import logger
from app.dao.agent_dao import agent_dao
from app.dao.okr_dao import okr_key_result_dao, okr_objective_dao
from app.dao.okr_settings_dao import okr_settings_dao
from app.dao.org_member_dao import org_member_dao
from app.dao.user_dao import user_dao
from app.records.okr import OKRKeyResultRecord, OKRObjectiveRecord

from .registry import ToolArguments


def _okr_access_module() -> ModuleType:
    return importlib.import_module("app.services.agent_tool_exec._agent_tool_exec_okr_access")


async def _get_okr(agent_id: uuid.UUID | None, arguments: ToolArguments) -> str:
    if not agent_id:
        return "OKR tools require agent context."
    try:
        agent = await agent_dao.get(agent_id)
        if not agent:
            return "Agent not found."

        tenant_id = agent.tenant_id
        if tenant_id is None:
            return "Agent has no tenant."
        settings = await okr_settings_dao.get_by_tenant(tenant_id)

        if not settings or not settings.enabled:
            return "OKR is not enabled for your organization."

        period_start_value = arguments.get("period_start")
        period_end_value = arguments.get("period_end")
        period_start = period_start_value if isinstance(period_start_value, str) else ""
        period_end = period_end_value if isinstance(period_end_value, str) else ""
        if period_start and period_end:
            ps = date.fromisoformat(period_start)
            pe = date.fromisoformat(period_end)
        else:
            ps, pe = _okr_access_module()._compute_okr_period_bounds(
                settings.period_frequency,
                settings.period_length_days,
            )

        objectives = list(
            await okr_objective_dao.list_for_period(
                tenant_id,
                period_start=ps,
                period_end=pe,
                exclude_archived=True,
            )
        )

        if not objectives:
            return f"No OKRs found for the current period ({ps} – {pe})."  # noqa: RUF001

        obj_ids = [objective.id for objective in objectives]
        all_krs = list(await okr_key_result_dao.list_for_objectives(obj_ids))

        krs_by_obj: dict[str, list[OKRKeyResultRecord]] = {}
        for kr in all_krs:
            krs_by_obj.setdefault(str(kr.objective_id), []).append(kr)

        user_owner_ids = [
            objective.owner_id for objective in objectives if objective.owner_type == "user" and objective.owner_id
        ]
        agent_owner_ids = [
            objective.owner_id for objective in objectives if objective.owner_type == "agent" and objective.owner_id
        ]

        user_names: dict[uuid.UUID, str] = {}
        if user_owner_ids:
            user_names = await user_dao.display_names_for_ids(user_owner_ids)
            unresolved_ids = [owner_id for owner_id in user_owner_ids if owner_id not in user_names]
            if unresolved_ids:
                member_names = await org_member_dao.names_for_ids(unresolved_ids)
                _ = user_names.update(member_names)

        agent_names: dict[uuid.UUID, str] = {}
        if agent_owner_ids:
            agent_names = await agent_dao.names_for_ids(agent_owner_ids)

        def _resolve_owner_label(obj: OKRObjectiveRecord) -> str:
            if obj.owner_type == "company":
                return "Company"
            if not obj.owner_id:
                return f"{obj.owner_type}:unassigned"
            if obj.owner_type == "user":
                return user_names.get(obj.owner_id) or f"user:{obj.owner_id}"
            if obj.owner_type == "agent":
                return agent_names.get(obj.owner_id) or f"agent:{obj.owner_id}"
            return f"{obj.owner_type}:{obj.owner_id}"

        lines = [f"# OKR Board - {ps} to {pe}\n"]

        company_objs = [objective for objective in objectives if objective.owner_type == "company"]
        member_objs = [objective for objective in objectives if objective.owner_type != "company"]

        if company_objs:
            lines.append("## Company Objectives")
            for objective in company_objs:
                krs = krs_by_obj.get(str(objective.id), [])
                pct = 0
                if krs:
                    pct = int(sum(min(kr.current_value / kr.target_value, 1) for kr in krs) / len(krs) * 100)
                lines.append(f"\n**O: {objective.title}** [{pct}%]  objective_id={objective.id}")
                lines.extend(
                    (
                        f"  - KR ({kr.status}): {kr.title}  "
                        + f"[{kr.current_value}/{kr.target_value} {kr.unit or ''}]  "
                        + f" kr_id={kr.id}"
                    )
                    for kr in krs
                )

        if member_objs:
            lines.append("\n## Member Objectives")
            for objective in member_objs:
                owner_label = _resolve_owner_label(objective)
                krs = krs_by_obj.get(str(objective.id), [])
                lines.append(f"\n**{owner_label}** | O: {objective.title}  objective_id={objective.id}")
                lines.extend(
                    (
                        f"  - KR ({kr.status}): {kr.title}  "
                        + f"[{kr.current_value}/{kr.target_value} {kr.unit or ''}]  "
                        + f" kr_id={kr.id}"
                    )
                    for kr in krs
                )

        return "\n".join(lines)

    except Exception as error:
        logger.exception(f"[OKR] get_okr failed for agent {agent_id}")
        return f"Failed to retrieve OKR data: {str(error)[:200]}"


async def _get_my_okr(agent_id: uuid.UUID | None, arguments: ToolArguments) -> str:
    del arguments
    if not agent_id:
        return "OKR tools require agent context."
    try:
        agent = await agent_dao.get(agent_id)
        if not agent:
            return "Agent not found."
        if agent.tenant_id is None:
            return "Agent has no tenant."

        settings = await okr_settings_dao.get_by_tenant(agent.tenant_id)
        if not settings or not settings.enabled:
            return "OKR is not enabled for your organization."

        ps, pe = _okr_access_module()._compute_okr_period_bounds(
            settings.period_frequency,
            settings.period_length_days,
        )

        all_objectives = await okr_objective_dao.list_for_period(
            agent.tenant_id,
            period_start=ps,
            period_end=pe,
            exclude_archived=True,
            owner_types=["agent"],
        )
        objectives = [obj for obj in all_objectives if obj.owner_id == agent_id]

        if not objectives:
            return (
                f"You have no OKRs set for the current period ({ps} – {pe}). "  # noqa: RUF001
                + "Contact the OKR Agent to set up your Objectives and Key Results."
            )

        obj_ids = [objective.id for objective in objectives]
        all_krs = list(await okr_key_result_dao.list_for_objectives(obj_ids))

        krs_by_obj: dict[str, list[OKRKeyResultRecord]] = {}
        for kr in all_krs:
            krs_by_obj.setdefault(str(kr.objective_id), []).append(kr)

        lines = [
            f"# My OKRs - {ps} to {pe}\n",
            "If you need to revise an existing OKR, reuse the IDs below:",
            "- change Objective title/description/status with update_objective(objective_id=...)",
            "- change KR title/target/unit/focus/status with update_kr_content(kr_id=...)",
            "- change KR numeric progress with update_kr_progress(kr_id=...)",
            "",
        ]
        for objective in objectives:
            krs = krs_by_obj.get(str(objective.id), [])
            lines.append(f"**O: {objective.title}**  objective_id={objective.id}")
            if objective.description:
                lines.append(f"  {objective.description}")
            lines.extend(
                (
                    f"  - [{kr.status}] {kr.title}  "
                    + f"Progress: {kr.current_value}/{kr.target_value} {kr.unit or ''}  "
                    + f"  kr_id={kr.id}"
                )
                for kr in krs
            )
        return "\n".join(lines)

    except Exception as error:
        logger.exception(f"[OKR] get_my_okr failed for agent {agent_id}")
        return f"Failed to retrieve your OKR: {str(error)[:200]}"


async def _get_okr_settings_tool(agent_id: uuid.UUID | None) -> str:
    if not agent_id:
        return "OKR tools require agent context."
    try:
        from app.services.okr_scheduler import get_okr_settings_for_agent

        agent = await agent_dao.get(agent_id)
        if not agent:
            return "Agent not found."
        tenant_id = agent.tenant_id
        if tenant_id is None:
            return "Agent has no tenant."

        settings = await get_okr_settings_for_agent(tenant_id)
        return json.dumps(settings, indent=2, ensure_ascii=False)

    except Exception as error:
        logger.exception(f"[OKR] get_okr_settings failed for agent {agent_id}")
        return f"Failed to get OKR settings: {str(error)[:200]}"
