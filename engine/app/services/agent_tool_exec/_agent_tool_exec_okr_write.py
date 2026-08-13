from __future__ import annotations

import importlib
import uuid
from datetime import UTC, datetime
from types import ModuleType

from app.core.logging import logger
from app.dao.okr_dao import okr_key_result_dao, okr_progress_log_dao

from .registry import ToolArguments, ToolArgumentValue


def _string_argument(arguments: ToolArguments, name: str, default: str = "") -> str:
    value = arguments.get(name, default)
    return value.strip() if isinstance(value, str) else default


def _float_value(value: ToolArgumentValue) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise TypeError("numeric value must be a string, integer, or float")
    return float(value)


def _okr_access_module() -> ModuleType:
    return importlib.import_module("app.services.agent_tool_exec._agent_tool_exec_okr_access")


def _okr_write_objectives_module() -> ModuleType:
    return importlib.import_module("app.services.agent_tool_exec.okr_write_objectives")


async def _update_kr_progress(agent_id: uuid.UUID | None, user_id: uuid.UUID | None, arguments: ToolArguments) -> str:
    if not agent_id:
        return "OKR tools require agent context."

    kr_id_str = _string_argument(arguments, "kr_id")
    value = arguments.get("value")
    note = arguments.get("note")

    if not kr_id_str:
        return "Missing required argument 'kr_id'. Call get_my_okr first to get your KR IDs."
    if value is None:
        return "Missing required argument 'value'."

    try:
        kr_id = uuid.UUID(kr_id_str)
    except ValueError:
        return f"Invalid kr_id format: {kr_id_str}"
    try:
        ctx = await _okr_access_module()._load_okr_request_context(None, agent_id, user_id)
        if not ctx["agent"]:
            return "Agent not found."
        if not ctx["tenant_id"]:
            return "Agent has no tenant."

        pair = await okr_key_result_dao.get_with_tenant(kr_id, ctx["tenant_id"])
        if not pair:
            return f"Key Result {kr_id_str} not found in your organization."

        kr, obj = pair
        permission_error = _okr_access_module()._can_access_existing_okr_target(
            ctx,
            obj.owner_type,
            obj.owner_id,
        )
        if permission_error:
            return permission_error

        prev_value = kr.current_value
        current_value = _float_value(value)

        ratio = current_value / kr.target_value if kr.target_value else 0
        if ratio >= 1.0:
            status = "completed"
        elif ratio >= 0.7:
            status = "on_track"
        elif ratio >= 0.4:
            status = "at_risk"
        else:
            status = "behind"

        now = datetime.now(UTC).replace(tzinfo=None)
        kr = await okr_key_result_dao.update(
            db_obj=kr,
            obj_in={
                "current_value": current_value,
                "last_updated_at": now,
                "status": status,
            },
        )
        await okr_progress_log_dao.create(
            obj_in={
                "kr_id": kr_id,
                "previous_value": prev_value,
                "new_value": current_value,
                "source": "self_report",
                "note": note,
            }
        )

        return f"KR updated: {kr.title}\n  {prev_value} → {value} {kr.unit or ''} (status: {kr.status})"

    except Exception as error:
        logger.exception(f"[OKR] update_kr_progress failed for agent {agent_id}")
        return f"Failed to update KR progress: {str(error)[:200]}"


async def _update_kr_content(agent_id: uuid.UUID | None, user_id: uuid.UUID | None, arguments: ToolArguments) -> str:
    if not agent_id:
        return "OKR tools require agent context."

    kr_id_str = _string_argument(arguments, "kr_id")
    if not kr_id_str:
        return "Missing required argument 'kr_id'. Call get_my_okr first to get your KR IDs."

    try:
        kr_id = uuid.UUID(kr_id_str)
    except ValueError:
        return f"Invalid kr_id format: {kr_id_str}"

    supported_fields = {
        "title": arguments.get("title"),
        "target_value": arguments.get("target_value"),
        "unit": arguments.get("unit"),
        "focus_ref": arguments.get("focus_ref"),
        "status": arguments.get("status"),
    }
    provided_updates = {key: value for key, value in supported_fields.items() if value is not None}
    if not provided_updates:
        return "No KR content fields provided. You can update: title, target_value, unit, focus_ref, status."
    try:
        ctx = await _okr_access_module()._load_okr_request_context(None, agent_id, user_id)
        if not ctx["agent"]:
            return "Agent not found."
        if not ctx["tenant_id"]:
            return "Agent has no tenant."

        pair = await okr_key_result_dao.get_with_tenant(kr_id, ctx["tenant_id"])
        if not pair:
            return f"Key Result {kr_id_str} not found in your organization."

        kr, obj = pair
        permission_error = _okr_access_module()._can_access_existing_okr_target(
            ctx,
            obj.owner_type,
            obj.owner_id,
        )
        if permission_error:
            return permission_error

        changed_fields: list[str] = []
        updates: dict[str, object] = {}
        if "title" in provided_updates:
            updates["title"] = str(provided_updates["title"]).strip()
            changed_fields.append("title")
        if "target_value" in provided_updates:
            updates["target_value"] = _float_value(provided_updates["target_value"])
            changed_fields.append("target_value")
        if "unit" in provided_updates:
            updates["unit"] = str(provided_updates["unit"]).strip() or None
            changed_fields.append("unit")
        if "focus_ref" in provided_updates:
            updates["focus_ref"] = str(provided_updates["focus_ref"]).strip() or None
            changed_fields.append("focus_ref")
        if "status" in provided_updates:
            updates["status"] = str(provided_updates["status"]).strip()
            changed_fields.append("status")

        kr = await okr_key_result_dao.update(db_obj=kr, obj_in=updates)
        return f"KR content updated: {kr.title}\nChanged fields: {', '.join(changed_fields)}"

    except Exception as error:
        logger.exception(f"[OKR] update_kr_content failed for agent {agent_id}")
        return f"Failed to update KR content: {str(error)[:200]}"


async def _update_any_kr_progress(
    agent_id: uuid.UUID | None, user_id: uuid.UUID | None, arguments: ToolArguments
) -> str:
    if not agent_id:
        return "OKR tools require agent context."
    try:
        ctx = await _okr_access_module()._load_okr_request_context(None, agent_id, user_id)
        if not ctx["agent"]:
            return "Agent not found."
        if not ctx["tenant_id"]:
            return "Agent has no tenant."

        kr_id_str = _string_argument(arguments, "kr_id")
        val = arguments.get("value")
        if not kr_id_str or val is None:
            return "Missing kr_id or value"
        try:
            kr_id = uuid.UUID(kr_id_str)
        except ValueError:
            return "Invalid formatted kr_id (must be UUID)"

        pair = await okr_key_result_dao.get_with_tenant(kr_id, ctx["tenant_id"])
        if not pair:
            return f"Key Result {kr_id} not found in your organization."

        kr, obj = pair
        permission_error = _okr_access_module()._can_access_existing_okr_target(
            ctx,
            obj.owner_type,
            obj.owner_id,
        )
        if permission_error:
            return permission_error

        old_val = kr.current_value
        current_value = _float_value(val)
        explicit_status = _string_argument(arguments, "status")
        if explicit_status:
            status = explicit_status
        else:
            progress = current_value / kr.target_value if kr.target_value != 0 else 0
            if progress >= 1.0:
                status = "completed"
            elif progress >= 0.7:
                status = "on_track"
            elif progress >= 0.4:
                status = "at_risk"
            else:
                status = "behind"

        now = datetime.now(UTC).replace(tzinfo=None)
        kr = await okr_key_result_dao.update(
            db_obj=kr,
            obj_in={
                "current_value": current_value,
                "status": status,
                "last_updated_at": now,
            },
        )
        note = _string_argument(arguments, "note", "Updated by OKR Agent after check-in")
        await okr_progress_log_dao.create(
            obj_in={
                "kr_id": kr.id,
                "previous_value": old_val,
                "new_value": kr.current_value,
                "source": "okr_agent" if ctx["agent_is_system"] else "agent",
                "note": note,
            }
        )

        return (
            f"Successfully updated KR '{kr.title}'. Progress: {old_val} -> {kr.current_value} "
            f"{kr.unit or ''}. Status: {kr.status}"
        )
    except Exception as error:
        logger.exception("[OKR] update_any_kr_progress failed")
        return f"Failed to update kr progress: {str(error)[:200]}"


async def _create_objective(agent_id: uuid.UUID | None, user_id: uuid.UUID | None, arguments: ToolArguments) -> str:
    return await _okr_write_objectives_module()._create_objective(agent_id, user_id, arguments)


async def _create_key_result(agent_id: uuid.UUID | None, user_id: uuid.UUID | None, arguments: ToolArguments) -> str:
    return await _okr_write_objectives_module()._create_key_result(agent_id, user_id, arguments)


async def _update_objective(agent_id: uuid.UUID | None, user_id: uuid.UUID | None, arguments: ToolArguments) -> str:
    return await _okr_write_objectives_module()._update_objective(agent_id, user_id, arguments)
