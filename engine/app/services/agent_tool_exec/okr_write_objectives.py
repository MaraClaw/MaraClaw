from __future__ import annotations

import importlib
import uuid
from datetime import date
from types import ModuleType

from app.core.logging import logger
from app.dao.agent_dao import agent_dao
from app.dao.okr_dao import okr_key_result_dao, okr_objective_dao
from app.dao.org_member_dao import org_member_dao
from app.dao.user_dao import user_dao

from .registry import ToolArguments


def _string_argument(arguments: ToolArguments, name: str) -> str | None:
    value = arguments.get(name)
    return value if isinstance(value, str) else None


def _float_argument(arguments: ToolArguments, name: str, default: float) -> float:
    value = arguments.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return default
    return float(value)


def _okr_access_module() -> ModuleType:
    return importlib.import_module("app.services.agent_tool_exec._agent_tool_exec_okr_access")


async def _create_objective(agent_id: uuid.UUID | None, user_id: uuid.UUID | None, arguments: ToolArguments) -> str:
    if not agent_id:
        return "OKR tools require agent context."
    try:
        ctx = await _okr_access_module()._load_okr_request_context(None, agent_id, user_id)
        ag = ctx["agent"]
        if not ag:
            return "Agent not found."

        title = _string_argument(arguments, "title")
        owner_type = _string_argument(arguments, "owner_type")
        period_start = _string_argument(arguments, "period_start")
        period_end = _string_argument(arguments, "period_end")
        if not title or not owner_type or not period_start or not period_end:
            return "Missing required fields: title, owner_type, period_start, period_end"
        p_start = date.fromisoformat(period_start)
        p_end = date.fromisoformat(period_end)

        owner_id_str = _string_argument(arguments, "owner_id")
        owner_name_hint = _string_argument(arguments, "owner_name")
        owner_id: uuid.UUID | None = None

        if owner_id_str:
            try:
                owner_id = uuid.UUID(owner_id_str)
            except ValueError:
                owner_id = None

            if owner_id:
                owner_exists = False
                if owner_type == "agent":
                    owner_exists = await agent_dao.get(owner_id) is not None
                elif owner_type == "user":
                    owner_exists = await user_dao.get(owner_id) is not None
                    if not owner_exists:
                        member = await org_member_dao.get(owner_id)
                        if member:
                            owner_exists = True
                            if member.user_id:
                                owner_id = member.user_id
                                logger.info(
                                    f"[OKR] _create_objective: resolved OrgMember.id {owner_id_str} "
                                    f"→ user_id {owner_id}"
                                )

                if not owner_exists:
                    owner_id = None
                    if not owner_name_hint:
                        return (
                            f"owner_id '{owner_id_str}' was not found. Provide a valid UUID, "
                            "or pass owner_name instead."
                        )

        if owner_type != "company" and not owner_id and owner_name_hint:
            if owner_type == "agent":
                if ag.tenant_id:
                    matches = await agent_dao.list_by_names_for_tenant(
                        ag.tenant_id, [owner_name_hint], exclude_stopped=False
                    )
                    owner_id = matches[0].id if matches else None
            elif owner_type == "user" and ag.tenant_id:
                user = await user_dao.find_by_username_or_display_name(owner_name_hint, tenant_id=ag.tenant_id)
                owner_id = user.id if user else None
                if not owner_id:
                    members = await org_member_dao.list_active_filtered(
                        tenant_id=ag.tenant_id, search=owner_name_hint, limit=20
                    )
                    exact = [m for m, _, _ in members if m.name == owner_name_hint]
                    if exact:
                        owner_id = exact[0].id
                    elif members:
                        owner_id = members[0][0].id

            if not owner_id:
                return f"Failed: Could not resolve a valid system UUID for the {owner_type} named '{owner_name_hint}'."

        if owner_type != "company" and not owner_id:
            return f"Failed: owner_id or owner_name is required for {owner_type} OKRs."

        if not ctx["agent_is_system"] and owner_type == "agent" and owner_id is None:
            owner_id = agent_id

        permission_error = _okr_access_module()._can_create_okr_target(ctx, owner_type, owner_id)
        if permission_error:
            return permission_error

        description = arguments.get("description")
        obj = await okr_objective_dao.create(
            obj_in={
                "tenant_id": ag.tenant_id,
                "title": title,
                "description": description if isinstance(description, str) else None,
                "owner_type": owner_type,
                "owner_id": owner_id,
                "period_start": p_start,
                "period_end": p_end,
                "status": "active",
            }
        )
        owner_info = f"owner={owner_name_hint or owner_id_str or 'unattributed'}"
        return f"Successfully created Objective '{obj.title}' (ID: {obj.id}, {owner_info})"
    except Exception as error:
        logger.exception("[OKR] create_objective failed")
        return f"Failed to create objective: {str(error)[:200]}"


async def _create_key_result(agent_id: uuid.UUID | None, user_id: uuid.UUID | None, arguments: ToolArguments) -> str:
    if not agent_id:
        return "OKR tools require agent context."
    try:
        ctx = await _okr_access_module()._load_okr_request_context(None, agent_id, user_id)
        if not ctx["agent"]:
            return "Agent not found."
        if not ctx["tenant_id"]:
            return "Agent has no tenant."

        obj_id_str = _string_argument(arguments, "objective_id")
        if not obj_id_str:
            return "Missing objective_id"
        try:
            obj_id = uuid.UUID(obj_id_str)
        except ValueError:
            return "Invalid formatted objective_id (must be UUID)"

        obj = await okr_objective_dao.get_for_tenant(obj_id, ctx["tenant_id"])
        if not obj:
            return f"Objective {obj_id} not found."

        permission_error = _okr_access_module()._can_access_existing_okr_target(
            ctx,
            obj.owner_type,
            obj.owner_id,
        )
        if permission_error:
            return permission_error

        kr = await okr_key_result_dao.create(
            obj_in={
                "objective_id": obj_id,
                "title": _string_argument(arguments, "title"),
                "target_value": _float_argument(arguments, "target_value", 100),
                "current_value": 0.0,
                "unit": _string_argument(arguments, "unit"),
                "focus_ref": _string_argument(arguments, "focus_ref"),
            }
        )
        return f"Successfully created Key Result '{kr.title}' (ID: {kr.id})"
    except Exception as error:
        logger.exception("[OKR] create_key_result failed")
        return f"Failed to create key result: {str(error)[:200]}"


async def _update_objective(agent_id: uuid.UUID | None, user_id: uuid.UUID | None, arguments: ToolArguments) -> str:
    if not agent_id:
        return "OKR tools require agent context."
    try:
        ctx = await _okr_access_module()._load_okr_request_context(None, agent_id, user_id)
        if not ctx["agent"]:
            return "Agent not found."
        if not ctx["tenant_id"]:
            return "Agent has no tenant."

        obj_id_str = _string_argument(arguments, "objective_id")
        if not obj_id_str:
            return "Missing objective_id"
        try:
            obj_id = uuid.UUID(obj_id_str)
        except ValueError:
            return "Invalid formatted objective_id (must be UUID)"

        obj = await okr_objective_dao.get_for_tenant(obj_id, ctx["tenant_id"])
        if not obj:
            return f"Objective {obj_id} not found."

        permission_error = _okr_access_module()._can_access_existing_okr_target(
            ctx,
            obj.owner_type,
            obj.owner_id,
        )
        if permission_error:
            return permission_error

        updates: dict[str, object] = {}
        changed: list[str] = []
        if "title" in arguments:
            title = _string_argument(arguments, "title")
            if title is not None:
                updates["title"] = title
                changed.append("title")
        if "description" in arguments:
            description = _string_argument(arguments, "description")
            if description is not None:
                updates["description"] = description
                changed.append("description")
        if "status" in arguments:
            status = _string_argument(arguments, "status")
            if status is not None:
                updates["status"] = status
                changed.append("status")
        if "period_start" in arguments:
            period_start = _string_argument(arguments, "period_start")
            if period_start is not None:
                updates["period_start"] = date.fromisoformat(period_start)
                changed.append("period_start")
        if "period_end" in arguments:
            period_end = _string_argument(arguments, "period_end")
            if period_end is not None:
                updates["period_end"] = date.fromisoformat(period_end)
                changed.append("period_end")

        if not changed:
            return "No supported fields provided to update."

        obj = await okr_objective_dao.update(db_obj=obj, obj_in=updates)
        return f"Successfully updated Objective {obj.id}. Changed fields: {', '.join(changed)}"
    except Exception as error:
        logger.exception("[OKR] update_objective failed")
        return f"Failed to update objective: {str(error)[:200]}"
