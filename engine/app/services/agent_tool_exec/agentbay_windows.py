from __future__ import annotations

import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from app.core.logging import logger

from .agentbay_apps import _agentbay_normalize_text
from .registry import ToolArguments

if TYPE_CHECKING:
    from app.services.agentbay_client import AgentBayClient


def _string_argument(arguments: ToolArguments, name: str, *, remove: bool = False) -> str:
    value = arguments.pop(name, "") if remove else arguments.get(name, "")
    return value if isinstance(value, str) else ""


def _integer_argument(arguments: ToolArguments, name: str, default: int = 0) -> int:
    value = arguments.get(name, default)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


async def _client(agent_id: uuid.UUID, arguments: ToolArguments) -> AgentBayClient:
    from app.services.agentbay_client import get_agentbay_client_for_agent

    return await get_agentbay_client_for_agent(
        agent_id, "computer", session_id=_string_argument(arguments, "_session_id", remove=True)
    )


async def _agentbay_computer_get_cursor_position(agent_id: uuid.UUID | None, ws: Path, arguments: ToolArguments) -> str:
    if not agent_id:
        return "AgentBay tools require agent context"
    try:
        result = await (await _client(agent_id, arguments)).computer_get_cursor_position()
        if result.get("success"):
            import json

            data = result.get("data")
            return f"Cursor position: {json.dumps(data, ensure_ascii=False) if isinstance(data, (dict, list)) else str(data)}"
        return f"Failed to get cursor position: {result.get('error_message', 'Unknown error')}"
    except RuntimeError as e:
        return f"{e!s}"
    except Exception as e:
        logger.exception("[AgentBay] Computer get_cursor_position failed")
        return f"Get cursor position failed: {str(e)[:200]}"


async def _agentbay_computer_get_active_window(agent_id: uuid.UUID | None, ws: Path, arguments: ToolArguments) -> str:
    if not agent_id:
        return "AgentBay tools require agent context"
    try:
        result = await (await _client(agent_id, arguments)).computer_get_active_window()
        if result.get("success"):
            import json

            window = result.get("window")
            return f"Active window:\n\n{json.dumps(window, ensure_ascii=False, indent=2) if isinstance(window, dict) else str(window)}"
        return f"Failed to get active window: {result.get('error_message', 'Unknown error')}"
    except RuntimeError as e:
        return f"{e!s}"
    except Exception as e:
        logger.exception("[AgentBay] Computer get_active_window failed")
        return f"Get active window failed: {str(e)[:200]}"


async def _agentbay_computer_list_windows(agent_id: uuid.UUID | None, ws: Path, arguments: ToolArguments) -> str:
    if not agent_id:
        return "AgentBay tools require agent context"
    try:
        result = await (await _client(agent_id, arguments)).computer_list_windows(
            timeout_ms=_integer_argument(arguments, "timeout_ms", 3000)
        )
        if result.get("success"):
            import json

            windows = result.get("windows", [])
            if not isinstance(windows, list) or not windows:
                return "No root windows found."
            return f"OS-level root desktop windows ({len(windows)}). These window_id values refer to whole application windows. Use them for activation, or for closing only when the user explicitly asked to close/quit an entire desktop window or app. Do NOT use these IDs for in-app popups, modals, embedded marketplace/store panels, browser/app tabs, document tabs, or software-internal dialogs; close those with the app UI, Escape, Ctrl+W, or agentbay_computer_dismiss_dialog.\n\n{json.dumps(windows, ensure_ascii=False, indent=2)[:5000]}"
        return f"Failed to list windows: {result.get('error_message', 'Unknown error')}"
    except RuntimeError as e:
        return f"{e!s}"
    except Exception as e:
        logger.exception("[AgentBay] Computer list_windows failed")
        return f"List windows failed: {str(e)[:200]}"


async def _agentbay_computer_activate_window(agent_id: uuid.UUID | None, ws: Path, arguments: ToolArguments) -> str:
    if not agent_id:
        return "AgentBay tools require agent context"
    window_id_value = arguments.get("window_id")
    window_id = window_id_value if isinstance(window_id_value, int) and not isinstance(window_id_value, bool) else None
    if window_id is None:
        return "Missing required argument 'window_id'"
    try:
        result = await (await _client(agent_id, arguments)).computer_activate_window(window_id)
        return (
            f"Window {window_id} activated (brought to front)"
            if result.get("success")
            else f"Failed to activate window {window_id}"
        )
    except RuntimeError as e:
        return f"{e!s}"
    except Exception as e:
        logger.exception("[AgentBay] Computer activate_window failed")
        return f"Activate window failed: {str(e)[:200]}"


async def _agentbay_computer_close_window(agent_id: uuid.UUID | None, ws: Path, arguments: ToolArguments) -> str:
    if not agent_id:
        return "AgentBay tools require agent context"
    window_id_value = arguments.get("window_id")
    window_id = window_id_value if isinstance(window_id_value, int) and not isinstance(window_id_value, bool) else None
    title = _string_argument(arguments, "title").strip()
    if window_id is None:
        if not title:
            return "Missing required argument `window_id`. Only use agentbay_computer_close_window when the user explicitly wants to close or quit an entire OS-level desktop window/application. If the target is an in-app popup, modal, embedded marketplace/store panel, browser/app tab, document tab, or software-internal dialog, use app UI controls, Escape, Ctrl+W, or agentbay_computer_dismiss_dialog instead."
        try:
            client = await _client(agent_id, arguments)
            windows_result = await client.computer_list_windows()
            if not windows_result.get("success"):
                return f"Failed to list windows before closing: {windows_result.get('error_message', 'Unknown error')}"
            import json
            from difflib import SequenceMatcher

            title_norm, candidates = _agentbay_normalize_text(title), []
            raw_windows = windows_result.get("windows", [])
            windows = list[object](raw_windows) if isinstance(raw_windows, list) else []
            for window in windows:
                if not isinstance(window, dict):
                    continue
                candidate = str(window.get("title") or window.get("window_title") or "")
                candidate_norm = _agentbay_normalize_text(candidate)
                if not candidate_norm:
                    continue
                score = (
                    0.95
                    if title_norm in candidate_norm or candidate_norm in title_norm
                    else SequenceMatcher(None, title_norm, candidate_norm).ratio()
                )
                if score >= 0.35:
                    item = dict[str, object](window)
                    item["match_score"] = round(score, 3)
                    candidates.append(item)
            candidates.sort(key=lambda item: item.get("match_score", 0), reverse=True)
            return f"Refusing to close by title-only match for `{title}` because it can close the wrong application. The candidates below are whole OS-level root windows. Choose a root window_id only if the user explicitly wants to close/quit that entire application window. For in-app popups, modals, embedded marketplace/store panels, browser/app tabs, document tabs, or software-internal dialogs, do not close a root window; use app UI controls, Escape, Ctrl+W, or agentbay_computer_dismiss_dialog instead.\n\n{json.dumps(candidates[:8], ensure_ascii=False, indent=2)[:3000]}"
        except RuntimeError as e:
            return f"{e!s}"
        except Exception as e:
            logger.exception("[AgentBay] Computer close_window candidate lookup failed")
            return f"Close window requires window_id. Candidate lookup failed: {str(e)[:200]}"
    try:
        result = await (await _client(agent_id, arguments)).computer_close_window(window_id)
        if result.get("success"):
            return f"Closed OS-level root desktop window {window_id}; the whole application window may now be gone. Call agentbay_computer_screenshot to verify."
        return f"Failed to close window {window_id}: {result.get('error_message', 'Unknown error')}"
    except RuntimeError as e:
        return f"{e!s}"
    except Exception as e:
        logger.exception("[AgentBay] Computer close_window failed")
        return f"Close window failed: {str(e)[:200]}"


async def _agentbay_computer_dismiss_dialog(agent_id: uuid.UUID | None, ws: Path, arguments: ToolArguments) -> str:
    if not agent_id:
        return "AgentBay tools require agent context"
    title, window_id = _string_argument(arguments, "title").strip(), arguments.get("window_id")
    try:
        client = await _client(agent_id, arguments)
        if window_id is not None:
            return "agentbay_computer_dismiss_dialog does not close root desktop windows. It only sends Escape to the active in-app popup/dialog. For in-app tabs, embedded panels, marketplace/store windows, or document tabs, use the app UI or shortcuts such as Ctrl+W. If the user explicitly wants to close/quit a whole desktop window or app, call agentbay_computer_close_window with a window_id returned by agentbay_computer_list_windows."
        result = await client.computer_press_keys(["esc"])
        if result.get("success"):
            title_note = f" Target hint: `{title}`." if title else ""
            return f"Sent Escape to safely dismiss the active in-app popup/dialog.{title_note} Call agentbay_computer_screenshot to verify. This tool never closes the root application window; if Escape does not affect an in-app tab or embedded panel, use that app's own close control or a shortcut such as Ctrl+W instead of root-window close."
        return f"Could not send Escape to dismiss the active popup/dialog: {result.get('error_message', 'Unknown error')}. Do not use this tool to close root application windows."
    except RuntimeError as e:
        return f"{e!s}"
    except Exception as e:
        logger.exception("[AgentBay] Computer dismiss_dialog failed")
        return f"Dismiss dialog failed: {str(e)[:200]}"
