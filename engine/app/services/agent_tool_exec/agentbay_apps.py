from __future__ import annotations

import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from app.core.json_types import JsonObject, json_as_str, json_object_from
from app.core.logging import logger

from .registry import ToolArguments

if TYPE_CHECKING:
    from app.services.agentbay_client import AgentBayClient

type _App = JsonObject


def _apps_from_response(value: object) -> list[_App]:
    if not isinstance(value, list):
        return []
    apps: list[_App] = []
    for item in list[object](value):
        if isinstance(item, dict):
            apps.append(json_object_from(item))
        elif item:
            apps.append({"name": str(item)})
    return apps


def _response_error(value: object, fallback: str = "Unknown error") -> str:
    return json_as_str(value) or fallback


def _agentbay_normalize_text(value: object) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _agentbay_app_field(app: _App, *keys: str) -> str:
    for key in keys:
        value = app.get(key)
        if value:
            return str(value)
    return ""


def _agentbay_format_apps(apps: list[_App], limit: int = 40) -> str:
    import json

    if not apps:
        return "[]"
    compact_apps: list[JsonObject | str] = []
    for app in apps[:limit]:
        compact: JsonObject = {}
        for key in (
            "name",
            "start_cmd",
            "startCmd",
            "work_directory",
            "workDirectory",
            "stop_cmd",
            "stopCmd",
        ):
            value = app.get(key)
            if value:
                compact[key] = value
        compact_apps.append(compact)
    rendered = json.dumps(compact_apps, ensure_ascii=False, indent=2)
    if len(apps) > limit:
        rendered += f"\n... {len(apps) - limit} more app(s) omitted"
    return rendered[:5000]


def _agentbay_find_installed_app_match(query: str, apps: list[_App]) -> tuple[_App | None, float]:
    from difflib import SequenceMatcher

    query_norm = _agentbay_normalize_text(query.split()[0] if query else query)
    if not query_norm:
        return None, 0.0
    best_app, best_score = None, 0.0
    for app in apps:
        for field in (
            _agentbay_app_field(app, "name"),
            _agentbay_app_field(app, "start_cmd", "startCmd"),
            _agentbay_app_field(app, "work_directory", "workDirectory"),
        ):
            field_norm = _agentbay_normalize_text(field)
            if not field_norm:
                continue
            if query_norm == field_norm:
                score = 1.0
            elif query_norm in field_norm or field_norm in query_norm:
                score = 0.9
            else:
                score = SequenceMatcher(None, query_norm, field_norm).ratio()
            if score > best_score:
                best_app, best_score = app, score
    return best_app, best_score


def _agentbay_uncertain_start_error(error_message: str) -> bool:
    text = (error_message or "").lower()
    return "may have launched" in text or "no processes found" in text


async def _agentbay_visible_apps_note(client: AgentBayClient) -> str:
    try:
        visible = await client.computer_list_visible_apps()
        if visible.get("success"):
            apps = _apps_from_response(visible.get("apps", []))
            return (
                f"Visible applications after the launch attempt ({len(apps)}):\n{_agentbay_format_apps(apps, limit=20)}"
            )
        error_message = _response_error(visible.get("error_message"))
        return f"Could not verify visible applications: {error_message}"
    except Exception as e:
        logger.debug(f"[AgentBay] Could not list visible apps after start_app: {e}")
        return f"Could not verify visible applications: {str(e)[:200]}"


async def _agentbay_computer_start_app(agent_id: uuid.UUID | None, ws: Path, arguments: ToolArguments) -> str:
    if not agent_id:
        return "AgentBay tools require agent context"
    from app.services.agentbay_client import get_agentbay_client_for_agent

    cmd_value = arguments.get("cmd")
    work_dir_value = arguments.get("work_dir")
    cmd = cmd_value if isinstance(cmd_value, str) else ""
    work_dir = work_dir_value if isinstance(work_dir_value, str) else ""
    if not cmd.strip():
        return "Missing required argument 'cmd'"
    try:
        session_id_value = arguments.pop("_session_id", "")
        session_id = session_id_value if isinstance(session_id_value, str) else ""
        client = await get_agentbay_client_for_agent(agent_id, "computer", session_id=session_id)
        result = await client.computer_start_app(cmd, work_dir=work_dir)
        if result.get("success"):
            data: object = result.get("data")
            if data is not None:
                try:
                    import json

                    data_str = (
                        json.dumps(data, ensure_ascii=False, indent=2)
                        if isinstance(data, (dict, list, str, int, float, bool))
                        else str(data)
                    )
                except (TypeError, ValueError):
                    data_str = str(data)
            else:
                data_str = ""
            return f"Application started: {cmd}" + (f"\n\n{data_str[:1000]}" if data_str else "")
        direct_error, installed_note = _response_error(result.get("error_message")), ""
        try:
            installed_result = await client.computer_get_installed_apps()
            if installed_result.get("success"):
                apps = _apps_from_response(installed_result.get("apps", []))
                matched_app, score = _agentbay_find_installed_app_match(cmd, apps)
                if matched_app and score >= 0.58:
                    matched_name = _agentbay_app_field(matched_app, "name") or "(unnamed app)"
                    matched_cmd = _agentbay_app_field(matched_app, "start_cmd", "startCmd")
                    matched_work_dir = _agentbay_app_field(matched_app, "work_directory", "workDirectory") or work_dir
                    if matched_cmd and matched_cmd.strip() != cmd.strip():
                        retry = await client.computer_start_app(matched_cmd, work_dir=matched_work_dir)
                        if retry.get("success"):
                            retry_data = retry.get("data")
                            retry_data_str = str(retry_data)[:1000] if retry_data is not None else ""
                            return (
                                f"Direct start command failed: {cmd}\nMatched installed app: {matched_name} (score={score:.2f})\nRetried with start_cmd: {matched_cmd}\nApplication started."
                                + (f"\n\n{retry_data_str}" if retry_data_str else "")
                            )
                        retry_error = _response_error(retry.get("error_message"))
                        if _agentbay_uncertain_start_error(retry_error):
                            return f"Direct start command failed: {cmd}\nMatched installed app: {matched_name} (score={score:.2f})\nRetried with start_cmd: {matched_cmd}\nRetry reported an uncertain launch result: {retry_error}\n\n{await _agentbay_visible_apps_note(client)}"
                        return f"Direct start command failed: {cmd}\nMatched installed app: {matched_name} (score={score:.2f})\nRetried with start_cmd: {matched_cmd}\nRetry failed: {retry_error}"
                installed_note = f"\n\nInstalled apps were checked, but no confident match was found for `{cmd}`. Use agentbay_computer_get_installed_apps and then pass the returned start_cmd to this tool."
            else:
                installed_note = (
                    "\n\nCould not check installed apps: "
                    + f"{_response_error(installed_result.get('error_message'))}"
                )
        except Exception as e:
            logger.debug(f"[AgentBay] Installed app fallback failed: {e}")
            installed_note = f"\n\nCould not check installed apps: {str(e)[:200]}"
        if _agentbay_uncertain_start_error(direct_error):
            return f"Start command reported an uncertain launch result: {direct_error}\n\n{await _agentbay_visible_apps_note(client)}{installed_note}"
        return f"Failed to start application: {direct_error}{installed_note}"
    except RuntimeError as e:
        return f"{e!s}"
    except Exception as e:
        logger.exception("[AgentBay] Computer start_app failed")
        return f"Start application failed: {str(e)[:200]}"


async def _agentbay_computer_get_installed_apps(agent_id: uuid.UUID | None, ws: Path, arguments: ToolArguments) -> str:
    if not agent_id:
        return "AgentBay tools require agent context"
    from app.services.agentbay_client import get_agentbay_client_for_agent

    try:
        session_id_value = arguments.pop("_session_id", "")
        session_id = session_id_value if isinstance(session_id_value, str) else ""
        client = await get_agentbay_client_for_agent(agent_id, "computer", session_id=session_id)
        result = await client.computer_get_installed_apps(
            start_menu=arguments.get("start_menu") is not False,
            desktop=arguments.get("desktop") is not False,
            ignore_system_apps=arguments.get("ignore_system_apps") is not False,
        )
        if result.get("success"):
            apps = _apps_from_response(result.get("apps", []))
            if not apps:
                return "No installed applications found."
            return f"Installed applications ({len(apps)}). Use the returned start_cmd exactly with agentbay_computer_start_app; do not guess app launch commands.\n\n{_agentbay_format_apps(apps, limit=80)}"
        error_message = _response_error(result.get("error_message"))
        return f"Failed to get installed applications: {error_message}"
    except RuntimeError as e:
        return f"{e!s}"
    except Exception as e:
        logger.exception("[AgentBay] Computer get_installed_apps failed")
        return f"Get installed applications failed: {str(e)[:200]}"


async def _agentbay_computer_list_visible_apps(agent_id: uuid.UUID | None, ws: Path, arguments: ToolArguments) -> str:
    if not agent_id:
        return "AgentBay tools require agent context"
    from app.services.agentbay_client import get_agentbay_client_for_agent

    try:
        session_id_value = arguments.pop("_session_id", "")
        session_id = session_id_value if isinstance(session_id_value, str) else ""
        client = await get_agentbay_client_for_agent(agent_id, "computer", session_id=session_id)
        result = await client.computer_list_visible_apps()
        if result.get("success"):
            import json

            apps = _apps_from_response(result.get("apps", []))
            return (
                "No visible applications running."
                if not apps
                else f"Visible applications ({len(apps)}):\n\n{json.dumps(apps, ensure_ascii=False, indent=2)[:3000]}"
            )
        error_message = _response_error(result.get("error_message"))
        return f"Failed to list applications: {error_message}"
    except RuntimeError as e:
        return f"{e!s}"
    except Exception as e:
        logger.exception("[AgentBay] Computer list_visible_apps failed")
        return f"List applications failed: {str(e)[:200]}"
