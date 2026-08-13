from __future__ import annotations

import importlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Final

from app.core.logging import logger
from app.dao.agent_dao import agent_dao
from app.dao.trigger_dao import agent_trigger_dao
from app.services import agent_tools
from app.services.agent_tool_exec.registry import ToolArguments, ToolArgumentValue, ToolOutputCallback, register
from app.services.focus_service import ensure_focus_item

MAX_TRIGGERS_PER_AGENT = 20
VALID_TRIGGER_TYPES: Final = {"cron", "once", "interval", "poll", "on_message", "webhook"}
_TRIGGER_HELPERS: Final = importlib.import_module("app.services.agent_tool_exec.trigger_helpers")


def _string_argument(arguments: ToolArguments, name: str) -> str:
    value = arguments.get(name)
    return value.strip() if isinstance(value, str) else ""


def _config_argument(arguments: ToolArguments, name: str) -> dict[str, ToolArgumentValue]:
    value = arguments.get(name)
    return dict(value) if isinstance(value, dict) else {}


async def _handle_set_trigger(
    agent_id: uuid.UUID,
    arguments: ToolArguments,
    *,
    session_id: str = "",
    user_id: uuid.UUID | None = None,
) -> str:

    name = _string_argument(arguments, "name")
    ttype = _string_argument(arguments, "type")
    config = _config_argument(arguments, "config")
    reason = _string_argument(arguments, "reason")
    focus_ref = _string_argument(arguments, "focus_ref") or _string_argument(arguments, "agenda_ref")

    if not name:
        return "❌ Missing required argument 'name'"
    if ttype not in VALID_TRIGGER_TYPES:
        return f"❌ Invalid trigger type '{ttype}'. Valid types: {', '.join(VALID_TRIGGER_TYPES)}"
    if not reason:
        return "❌ Missing required argument 'reason'"

    try:
        focus_ref = await ensure_focus_item(agent_id, focus_ref=focus_ref, description=reason or name, system=False)
    except Exception as error:
        logger.warning(f"[Trigger] Failed to ensure Focus item for trigger {name}: {error}")
        focus_ref = focus_ref or name

    validation_error = _TRIGGER_HELPERS._validate_trigger_config(ttype, config)
    if validation_error:
        return validation_error
    if ttype == "on_message":
        await _TRIGGER_HELPERS._snapshot_latest_message(agent_tools, agent_id, config)
    elif ttype == "webhook":
        config["token"] = secrets.token_urlsafe(8)

    await _TRIGGER_HELPERS._record_origin_metadata(
        agent_tools, agent_id, config, session_id=session_id, user_id=user_id
    )

    try:
        agent = await agent_dao.get(agent_id)
        agent_max_triggers = (agent.max_triggers if agent else None) or MAX_TRIGGERS_PER_AGENT

        count = await agent_trigger_dao.count_enabled_for_agent(agent_id)
        if count >= agent_max_triggers:
            return f"❌ Maximum trigger limit reached ({agent_max_triggers}). Cancel some triggers first."

        existing = await agent_trigger_dao.get_by_agent_and_name(agent_id, name)
        if existing:
            if existing.is_enabled:
                return (
                    f"❌ Trigger '{name}' already exists and is active. "
                    "Use update_trigger to modify it, or cancel_trigger first."
                )
            if ttype == "webhook":
                old_token = (existing.config or {}).get("token")
                if old_token:
                    config["token"] = old_token
            updates: dict = {
                "type": ttype,
                "config": config,
                "reason": reason,
                "focus_ref": focus_ref,
                "is_enabled": True,
            }
            if existing.max_fires and existing.fire_count >= existing.max_fires:
                updates["fire_count"] = 0
            existing = await agent_trigger_dao.update(db_obj=existing, obj_in=updates) or existing
            return (
                f"✅ Trigger '{name}' re-enabled with new configuration "
                f"({ttype}, fired {existing.fire_count} times so far)"
            )

        obj_in: dict = {
            "agent_id": agent_id,
            "name": name,
            "type": ttype,
            "config": config,
            "reason": reason,
            "focus_ref": focus_ref,
        }
        if ttype == "on_message":
            obj_in["max_fires"] = 100
            obj_in["expires_at"] = datetime.now(UTC) + timedelta(days=7)
        await agent_trigger_dao.create(obj_in=obj_in)

        await _TRIGGER_HELPERS._write_trigger_audit(
            "trigger_created",
            {"name": name, "type": ttype, "reason": reason[:100]},
            agent_id=agent_id,
        )
        if ttype == "webhook":
            from app.services.platform_service import platform_service

            base = await platform_service.get_public_base_url()
            webhook_url = f"{base.rstrip('/')}/api/webhooks/t/{config['token']}"
            return (
                f"✅ Webhook trigger '{name}' created.\n\nWebhook URL: {webhook_url}\n\n"
                "Tell the user to configure this URL in their external service (e.g. GitHub, Grafana). "
                "When the service sends a POST to this URL, you will be woken up with the payload as context."
            )
        return (
            f"✅ Trigger '{name}' created ({ttype}). "
            "It will fire according to your config and wake you up with the reason as context."
        )
    except Exception as error:
        return f"❌ Failed to create trigger: {error}"


async def _handle_update_trigger(agent_id: uuid.UUID, arguments: ToolArguments) -> str:
    name = _string_argument(arguments, "name")
    if not name:
        return "❌ Missing required argument 'name'"

    new_config = _config_argument(arguments, "config") if "config" in arguments else None
    new_reason = _string_argument(arguments, "reason") if "reason" in arguments else None
    if new_config is None and new_reason is None:
        return "❌ Provide at least one of 'config' or 'reason' to update"

    try:
        trigger = await agent_trigger_dao.get_by_agent_and_name(agent_id, name)
        if not trigger:
            return f"❌ Trigger '{name}' not found"

        changes = []
        updates: dict = {}
        if new_config is not None:
            old_config = trigger.config
            updates["config"] = new_config
            changes.append(f"config: {old_config} \u2192 {new_config}")
        if new_reason is not None:
            updates["reason"] = new_reason
            changes.append("reason updated")
        await agent_trigger_dao.update(db_obj=trigger, obj_in=updates)

        await _TRIGGER_HELPERS._write_trigger_audit(
            "trigger_updated",
            {"name": name, "changes": "; ".join(changes)},
            agent_id=agent_id,
        )
        return f"✅ Trigger '{name}' updated: {'; '.join(changes)}"
    except Exception as error:
        return f"❌ Failed to update trigger: {error}"


async def _handle_cancel_trigger(agent_id: uuid.UUID, arguments: ToolArguments) -> str:
    name = _string_argument(arguments, "name")
    if not name:
        return "❌ Missing required argument 'name'"

    try:
        trigger = await agent_trigger_dao.get_by_agent_and_name(agent_id, name)
        if not trigger:
            return f"❌ Trigger '{name}' not found"
        if not trigger.is_enabled:
            return f"\u2139\ufe0f Trigger '{name}' is already disabled"

        await agent_trigger_dao.update(db_obj=trigger, obj_in={"is_enabled": False})

        await _TRIGGER_HELPERS._write_trigger_audit("trigger_cancelled", {"name": name}, agent_id=agent_id)
        return f"✅ Trigger '{name}' cancelled. It will no longer fire."
    except Exception as error:
        return f"❌ Failed to cancel trigger: {error}"


async def _handle_list_triggers(agent_id: uuid.UUID) -> str:
    try:
        triggers = await agent_trigger_dao.list_for_agent(agent_id)

        if not triggers:
            return "No triggers found. Use set_trigger to create one."

        lines = [
            "| Name | Type | Config | Reason | Status | Fires |",
            "|------|------|--------|--------|--------|-------|",
        ]
        for trigger in triggers:
            status = "✅ active" if trigger.is_enabled else "⏸ disabled"
            config_str = str(trigger.config)[:50]
            reason_str = trigger.reason[:40] if trigger.reason else ""
            lines.append(
                f"| {trigger.name} | {trigger.type} | {config_str} | {reason_str} | {status} | {trigger.fire_count} |"
            )
        return "\n".join(lines)
    except Exception as error:
        return f"❌ Failed to list triggers: {error}"


@register("set_trigger")
async def set_trigger(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del on_output
    return await _handle_set_trigger(agent_id, arguments, session_id=session_id, user_id=user_id)


@register("update_trigger")
async def update_trigger(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, session_id, on_output
    return await _handle_update_trigger(agent_id, arguments)


@register("cancel_trigger")
async def cancel_trigger(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, session_id, on_output
    return await _handle_cancel_trigger(agent_id, arguments)


@register("list_triggers")
async def list_triggers(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del arguments, user_id, session_id, on_output
    return await _handle_list_triggers(agent_id)
