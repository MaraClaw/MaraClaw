from __future__ import annotations

import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, TypedDict

import pytest

from app.services.agent_tool_exec import agentbay_apps, agentbay_computer, agentbay_screen, agentbay_windows
from app.services.agent_tool_exec.registry import ToolArguments


class ScreenSize(TypedDict):
    width: int
    height: int


class ScreenSizeResponse(TypedDict):
    success: bool
    data: ScreenSize


class InstalledApp(TypedDict):
    name: str
    start_cmd: str


class StartAppResponse(TypedDict):
    success: bool
    error_message: str


class InstalledAppsResponse(TypedDict):
    success: bool
    apps: list[InstalledApp]


class Window(TypedDict):
    window_id: int
    title: str


class WindowsResponse(TypedDict):
    success: bool
    windows: list[Window]


class ScreenshotResponse(TypedDict):
    success: bool
    data: bytes


class InvalidScreenSize(TypedDict):
    width: str
    height: int


class InvalidScreenSizeResponse(TypedDict):
    success: bool
    data: InvalidScreenSize


class VisibleApp(TypedDict):
    name: str


class VisibleAppsResponse(TypedDict):
    success: bool
    apps: list[VisibleApp]


class SuccessResponse(TypedDict):
    success: bool


class GridOptions(TypedDict):
    origin_x: int
    origin_y: int
    minor_step: int
    major_step: int
    pixel_scale: int


type DesktopCall = tuple[Literal["close"], int] | tuple[Literal["keys"], list[str]]


@pytest.mark.asyncio
async def test_desktop_click_rejects_out_of_bounds_coordinates(monkeypatch, tmp_path: Path) -> None:
    class Client:
        async def computer_get_screen_size(self) -> ScreenSizeResponse:
            return {"success": True, "data": {"width": 100, "height": 80}}

    async def get_client(_agent_id: uuid.UUID, _environment: str, *, session_id: str) -> Client:
        return Client()

    monkeypatch.setitem(
        sys.modules, "app.services.agentbay_client", SimpleNamespace(get_agentbay_client_for_agent=get_client)
    )
    arguments: ToolArguments = {"x": 100, "y": 1}
    result = await agentbay_computer._agentbay_computer_click(uuid.uuid4(), tmp_path, arguments)

    assert (
        result
        == "Click refused: (100, 1) is outside the Cloud Desktop coordinate system (width=100, height=80). Use coordinates from the latest full desktop screenshot."
    )


@pytest.mark.asyncio
async def test_apps_retry_and_windows_title_only_refusal_use_fake_client(monkeypatch, tmp_path: Path) -> None:
    class Client:
        async def computer_start_app(self, command: str, *, work_dir: str) -> StartAppResponse:
            return {"success": command == "firefox", "error_message": "not found"}

        async def computer_get_installed_apps(self) -> InstalledAppsResponse:
            return {"success": True, "apps": [{"name": "Firefox", "start_cmd": "firefox"}]}

        async def computer_list_windows(self) -> WindowsResponse:
            return {"success": True, "windows": [{"window_id": 7, "title": "Firefox"}]}

    async def get_client(_agent_id: uuid.UUID, _environment: str, *, session_id: str) -> Client:
        return Client()

    monkeypatch.setitem(
        sys.modules, "app.services.agentbay_client", SimpleNamespace(get_agentbay_client_for_agent=get_client)
    )
    agent_id = uuid.uuid4()
    start_arguments: ToolArguments = {"cmd": "Firefox", "_session_id": "chat"}
    close_arguments: ToolArguments = {"title": "Firefox", "_session_id": "chat"}
    started = await agentbay_apps._agentbay_computer_start_app(agent_id, tmp_path, start_arguments)
    refused = await agentbay_windows._agentbay_computer_close_window(agent_id, tmp_path, close_arguments)

    assert "Retried with start_cmd: firefox" in started
    assert "Refusing to close by title-only match" in refused


@pytest.mark.asyncio
async def test_screenshot_grid_and_precision_aliases_preserve_argument_copy(monkeypatch, tmp_path: Path) -> None:
    injected: list[tuple[bytes, GridOptions]] = []
    seen_screenshot_arguments: list[ToolArguments] = []

    class Client:
        async def computer_screenshot(self) -> ScreenshotResponse:
            return {"success": True, "data": b"raw"}

        async def computer_get_screen_size(self) -> InvalidScreenSizeResponse:
            return {"success": True, "data": {"width": "bad", "height": 100}}

    async def get_client(_agent_id: uuid.UUID, _environment: str, *, session_id: str) -> Client:
        return Client()

    async def fake_screenshot(_agent_id: uuid.UUID, _workspace: Path, arguments: ToolArguments) -> str:
        seen_screenshot_arguments.append(arguments)
        return "nested screenshot"

    def store_temp_screenshot(data: bytes, *, grid_options: GridOptions) -> str:
        injected.append((data, grid_options))
        return "grid-id"

    def crop_image_bytes(
        _raw_bytes: bytes, *, x: int, y: int, width: int, height: int
    ) -> tuple[bytes, tuple[int, int, int, int], int]:
        return b"crop", (x, y, width, height), 3

    def image_dimensions(_data: bytes) -> tuple[int, int]:
        return 20, 10

    monkeypatch.setitem(
        sys.modules, "app.services.agentbay_client", SimpleNamespace(get_agentbay_client_for_agent=get_client)
    )
    monkeypatch.setitem(
        sys.modules,
        "app.services.vision_inject",
        SimpleNamespace(store_temp_screenshot=store_temp_screenshot),
    )
    monkeypatch.setattr(agentbay_screen, "_agentbay_crop_image_bytes", crop_image_bytes)
    monkeypatch.setattr(agentbay_screen, "_agentbay_image_dimensions", image_dimensions)
    agent_id = uuid.uuid4()
    screenshot_arguments: ToolArguments = {"focus_x": 4, "focus_y": 5, "focus_width": 6, "focus_height": 7}
    screenshot = await agentbay_screen._agentbay_computer_screenshot(agent_id, tmp_path, screenshot_arguments)
    monkeypatch.setattr(agentbay_screen, "_agentbay_computer_screenshot", fake_screenshot)
    precision_arguments: ToolArguments = {"focus_x": 1, "focus_y": 2, "focus_width": 3, "focus_height": 4}
    precision = await agentbay_screen._agentbay_computer_precision_screenshot(agent_id, tmp_path, precision_arguments)

    assert agentbay_screen._agentbay_extract_screen_dimensions({"width": "bad", "height": 100}) == (None, None, "")
    assert "[ImageID: grid-id]" in screenshot
    assert injected == [(b"crop", {"origin_x": 4, "origin_y": 5, "minor_step": 10, "major_step": 50, "pixel_scale": 3})]
    assert precision.startswith("Precision desktop crop captured")
    assert precision_arguments["x"] == 1
    assert precision_arguments["width"] == 3
    assert seen_screenshot_arguments[0] is not precision_arguments
    assert seen_screenshot_arguments[0]["focus_width"] == 360


@pytest.mark.asyncio
async def test_uncertain_app_start_root_close_escape_and_visible_apps_use_fake_client(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[DesktopCall] = []

    class Client:
        async def computer_start_app(self, _command: str, *, work_dir: str) -> StartAppResponse:
            return {"success": False, "error_message": "may have launched"}

        async def computer_get_installed_apps(self) -> InstalledAppsResponse:
            return {"success": True, "apps": []}

        async def computer_list_visible_apps(self) -> VisibleAppsResponse:
            return {"success": True, "apps": [{"name": "Firefox"}]}

        async def computer_close_window(self, window_id: int) -> SuccessResponse:
            calls.append(("close", window_id))
            return {"success": True}

        async def computer_press_keys(self, keys: list[str]) -> SuccessResponse:
            calls.append(("keys", keys))
            return {"success": True}

    async def get_client(_agent_id: uuid.UUID, _environment: str, *, session_id: str) -> Client:
        return Client()

    monkeypatch.setitem(
        sys.modules, "app.services.agentbay_client", SimpleNamespace(get_agentbay_client_for_agent=get_client)
    )
    agent_id = uuid.uuid4()
    start_arguments: ToolArguments = {"cmd": "unknown"}
    close_arguments: ToolArguments = {"window_id": 9}
    dismiss_arguments: ToolArguments = {"title": "popup"}
    visible_arguments: ToolArguments = {}
    uncertain = await agentbay_apps._agentbay_computer_start_app(agent_id, tmp_path, start_arguments)
    closed = await agentbay_windows._agentbay_computer_close_window(agent_id, tmp_path, close_arguments)
    dismissed = await agentbay_windows._agentbay_computer_dismiss_dialog(agent_id, tmp_path, dismiss_arguments)
    visible = await agentbay_apps._agentbay_computer_list_visible_apps(agent_id, tmp_path, visible_arguments)

    assert "uncertain launch result" in uncertain
    assert "Visible applications after" in uncertain
    assert closed.startswith("Closed OS-level root desktop window 9")
    assert dismissed.startswith("Sent Escape")
    assert "Visible applications (1)" in visible
    assert "Firefox" in visible
    assert calls == [("close", 9), ("keys", ["esc"])]
