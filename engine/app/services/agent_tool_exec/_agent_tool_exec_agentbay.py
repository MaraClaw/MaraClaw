from __future__ import annotations

import importlib
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Final, cast

from app.config import get_settings
from app.core.json_types import object_attr

from . import workspace_paths
from .registry import ToolArguments, ToolOutputCallback, current_execution_context, register

type AgentbayHelper = Callable[[uuid.UUID, Path, ToolArguments], Awaitable[str]]

_AGENTBAY_HELPERS: Final[tuple[tuple[str, str], ...]] = (
    ("agentbay_browser_navigate", "_agentbay_browser_navigate"),
    ("agentbay_browser_screenshot", "_agentbay_browser_screenshot"),
    ("agentbay_browser_save_screenshot", "_agentbay_browser_save_screenshot"),
    ("agentbay_browser_click", "_agentbay_browser_click"),
    ("agentbay_browser_type", "_agentbay_browser_type"),
    ("agentbay_code_execute", "_agentbay_code_execute"),
    ("agentbay_code_write_file", "_agentbay_code_write_file"),
    ("agentbay_code_read_file", "_agentbay_code_read_file"),
    ("agentbay_code_edit_file", "_agentbay_code_edit_file"),
    ("agentbay_browser_extract", "_agentbay_browser_extract"),
    ("agentbay_browser_observe", "_agentbay_browser_observe"),
    ("agentbay_browser_login", "_agentbay_browser_login"),
    ("agentbay_command_exec", "_agentbay_command_exec"),
    ("agentbay_computer_screenshot", "_agentbay_computer_screenshot"),
    ("agentbay_computer_save_screenshot", "_agentbay_computer_save_screenshot"),
    ("agentbay_computer_precision_screenshot", "_agentbay_computer_precision_screenshot"),
    ("agentbay_computer_click", "_agentbay_computer_click"),
    ("agentbay_computer_input_text", "_agentbay_computer_input_text"),
    ("agentbay_computer_press_keys", "_agentbay_computer_press_keys"),
    ("agentbay_computer_scroll", "_agentbay_computer_scroll"),
    ("agentbay_computer_move_mouse", "_agentbay_computer_move_mouse"),
    ("agentbay_computer_drag_mouse", "_agentbay_computer_drag_mouse"),
    ("agentbay_computer_get_screen_size", "_agentbay_computer_get_screen_size"),
    ("agentbay_computer_start_app", "_agentbay_computer_start_app"),
    ("agentbay_computer_get_installed_apps", "_agentbay_computer_get_installed_apps"),
    ("agentbay_computer_get_cursor_position", "_agentbay_computer_get_cursor_position"),
    ("agentbay_computer_get_active_window", "_agentbay_computer_get_active_window"),
    ("agentbay_computer_list_windows", "_agentbay_computer_list_windows"),
    ("agentbay_computer_activate_window", "_agentbay_computer_activate_window"),
    ("agentbay_computer_close_window", "_agentbay_computer_close_window"),
    ("agentbay_computer_dismiss_dialog", "_agentbay_computer_dismiss_dialog"),
    ("agentbay_computer_list_visible_apps", "_agentbay_computer_list_visible_apps"),
    ("agentbay_file_transfer", "_agentbay_file_transfer"),
)

_AGENTBAY_HELPER_MODULES: Final[dict[str, str]] = {
    "_agentbay_browser_navigate": "app.services.agent_tool_exec.agentbay_browser",
    "_agentbay_browser_screenshot": "app.services.agent_tool_exec.agentbay_browser",
    "_agentbay_browser_save_screenshot": "app.services.agent_tool_exec.agentbay_browser",
    "_agentbay_browser_click": "app.services.agent_tool_exec.agentbay_browser",
    "_agentbay_browser_type": "app.services.agent_tool_exec.agentbay_browser",
    "_agentbay_code_execute": "app.services.agent_tool_exec.agentbay_code",
    "_agentbay_code_write_file": "app.services.agent_tool_exec.agentbay_code",
    "_agentbay_code_read_file": "app.services.agent_tool_exec.agentbay_code",
    "_agentbay_code_edit_file": "app.services.agent_tool_exec.agentbay_code",
    "_agentbay_browser_extract": "app.services.agent_tool_exec.agentbay_browser",
    "_agentbay_browser_observe": "app.services.agent_tool_exec.agentbay_browser",
    "_agentbay_browser_login": "app.services.agent_tool_exec.agentbay_browser",
    "_agentbay_command_exec": "app.services.agent_tool_exec.agentbay_code",
    "_agentbay_computer_screenshot": "app.services.agent_tool_exec.agentbay_screen",
    "_agentbay_computer_save_screenshot": "app.services.agent_tool_exec.agentbay_screen",
    "_agentbay_computer_precision_screenshot": "app.services.agent_tool_exec.agentbay_screen",
    "_agentbay_computer_click": "app.services.agent_tool_exec.agentbay_computer",
    "_agentbay_computer_input_text": "app.services.agent_tool_exec.agentbay_computer",
    "_agentbay_computer_press_keys": "app.services.agent_tool_exec.agentbay_computer",
    "_agentbay_computer_scroll": "app.services.agent_tool_exec.agentbay_computer",
    "_agentbay_computer_move_mouse": "app.services.agent_tool_exec.agentbay_computer",
    "_agentbay_computer_drag_mouse": "app.services.agent_tool_exec.agentbay_computer",
    "_agentbay_computer_get_screen_size": "app.services.agent_tool_exec.agentbay_computer",
    "_agentbay_computer_start_app": "app.services.agent_tool_exec.agentbay_apps",
    "_agentbay_computer_get_installed_apps": "app.services.agent_tool_exec.agentbay_apps",
    "_agentbay_computer_get_cursor_position": "app.services.agent_tool_exec.agentbay_windows",
    "_agentbay_computer_get_active_window": "app.services.agent_tool_exec.agentbay_windows",
    "_agentbay_computer_list_windows": "app.services.agent_tool_exec.agentbay_windows",
    "_agentbay_computer_activate_window": "app.services.agent_tool_exec.agentbay_windows",
    "_agentbay_computer_close_window": "app.services.agent_tool_exec.agentbay_windows",
    "_agentbay_computer_dismiss_dialog": "app.services.agent_tool_exec.agentbay_windows",
    "_agentbay_computer_list_visible_apps": "app.services.agent_tool_exec.agentbay_apps",
    "_agentbay_file_transfer": "app.services.agent_tool_exec.agentbay_files",
}


def _load_agentbay_helper(helper_name: str) -> AgentbayHelper:
    module = importlib.import_module(_AGENTBAY_HELPER_MODULES[helper_name])
    loaded = object_attr(module, helper_name)
    if not callable(loaded):
        raise TypeError(f"AgentBay helper {helper_name} is not callable")
    return cast(AgentbayHelper, loaded)


_settings = get_settings()
_WORKSPACE_ROOT: Final[Path] = Path(_settings.STORAGE_LOCAL_ROOT or _settings.AGENT_DATA_DIR)


def _register_agentbay_handler(tool_name: str, helper_name: str) -> None:
    @register(tool_name)
    async def handler(
        *,
        arguments: ToolArguments,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        session_id: str,
        on_output: ToolOutputCallback | None,
    ) -> str:
        del user_id, session_id, on_output
        context = current_execution_context()
        ws = (
            context.workspace_root
            if context is not None and context.workspace_root is not None
            else workspace_paths._agent_workspace_root(agent_id, workspace_root=_WORKSPACE_ROOT)
        )
        helper = _load_agentbay_helper(helper_name)
        return await helper(agent_id, ws, arguments)


for _tool_name, _helper_name in _AGENTBAY_HELPERS:
    _register_agentbay_handler(_tool_name, _helper_name)
