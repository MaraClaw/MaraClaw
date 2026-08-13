from __future__ import annotations

import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, TypedDict

import pytest

from app.services import agent_tools
from app.services.agent_tool_exec import agentbay_browser, agentbay_code
from app.services.agent_tool_exec.agentbay_response import _agentbay_response_list, _agentbay_response_text
from app.services.agent_tool_exec.registry import ToolArguments


class BrowserNavigateResponse(TypedDict):
    title: str
    content: str
    screenshot: bytes


class BrowserScreenshotResponse(TypedDict):
    screenshot: bytes


class CodeExecuteResponse(TypedDict):
    stdout: str
    exit_code: int


class CommandExecResponse(TypedDict):
    success: bool
    exit_code: int
    stdout: str


class FileEdit(TypedDict):
    oldText: str
    newText: str


type BrowserCall = tuple[str] | tuple[str, str, bool]
type CodeCall = (
    tuple[Literal["execute"], str, str, int]
    | tuple[Literal["read"], str]
    | tuple[Literal["edit"], str, list[FileEdit], bool]
    | tuple[Literal["command"], str, int, str]
    | tuple[Literal["write"], str, str, str]
)


def test_agentbay_response_text_preserves_string_identity_and_uses_fallback() -> None:
    response_text = "response text"
    fallback = "fallback text"

    assert _agentbay_response_text(response_text, fallback) is response_text
    assert _agentbay_response_text(b"response text", fallback) is fallback
    assert _agentbay_response_text(42, fallback) is fallback
    assert _agentbay_response_text({"name": "Firefox"}, fallback) is fallback


def test_agentbay_response_list_preserves_list_identity_and_uses_empty_list() -> None:
    apps = [{"name": "Firefox"}]

    assert _agentbay_response_list(apps) is apps
    assert _agentbay_response_list("not a list") == []
    assert _agentbay_response_list(b"not a list") == []
    assert _agentbay_response_list(42) == []
    assert _agentbay_response_list({"name": "Firefox"}) == []


@pytest.mark.asyncio
async def test_browser_click_and_facade_use_fake_client_without_optional_sdk(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []

    class Client:
        async def browser_click(self, selector: str) -> None:
            calls.append(("click", selector))

    async def get_client(_agent_id: uuid.UUID, _environment: str, *, session_id: str) -> Client:
        return Client()

    monkeypatch.setitem(
        sys.modules, "app.services.agentbay_client", SimpleNamespace(get_agentbay_client_for_agent=get_client)
    )
    arguments: ToolArguments = {"selector": "#submit", "_session_id": "chat"}
    result = await agent_tools._agentbay_browser_click(uuid.uuid4(), tmp_path, arguments)

    assert result == "✅ Clicked element: #submit"
    assert calls == [("click", "#submit")]
    assert "_session_id" not in arguments


@pytest.mark.asyncio
async def test_browser_facade_defers_owner_lookup_until_direct_call(monkeypatch, tmp_path: Path) -> None:
    async def fake_owner(agent_id: uuid.UUID | None, workspace: Path, arguments: ToolArguments) -> str:
        assert agent_id is not None
        assert workspace == tmp_path
        assert arguments == {"selector": "#deferred"}
        return "patched owner"

    monkeypatch.setattr(agentbay_browser, "_agentbay_browser_click", fake_owner)

    arguments: ToolArguments = {"selector": "#deferred"}

    assert await agent_tools._agentbay_browser_click(uuid.uuid4(), tmp_path, arguments) == "patched owner"


@pytest.mark.asyncio
async def test_code_write_uses_fake_filesystem_and_keeps_optional_imports_lazy(monkeypatch, tmp_path: Path) -> None:
    writes: list[tuple[str, str, str]] = []

    class Filesystem:
        def write_file(self, path: str, content: str, mode: str) -> SimpleNamespace:
            writes.append((path, content, mode))
            return SimpleNamespace(success=True, error_message="")

    class Client:
        _session = SimpleNamespace(file_system=Filesystem())

    async def get_client(_agent_id: uuid.UUID, _environment: str, *, session_id: str) -> Client:
        return Client()

    monkeypatch.setitem(
        sys.modules, "app.services.agentbay_client", SimpleNamespace(get_agentbay_client_for_agent=get_client)
    )
    arguments: ToolArguments = {"remote_path": "/sandbox/a.txt", "content": "abc", "_session_id": "chat"}
    result = await agentbay_code._agentbay_code_write_file(uuid.uuid4(), tmp_path, arguments)

    assert result == "File written in AgentBay Code Sandbox: /sandbox/a.txt (3 bytes, mode=overwrite)"
    assert writes == [("/sandbox/a.txt", "abc", "overwrite")]
    assert "PIL" not in agentbay_browser.__dict__


@pytest.mark.asyncio
async def test_browser_navigate_save_and_client_failure_use_only_fakes(monkeypatch, tmp_path: Path) -> None:
    calls: list[BrowserCall] = []

    class Client:
        async def browser_navigate(self, url: str, *, wait_for: str, screenshot: bool) -> BrowserNavigateResponse:
            calls.append((url, wait_for, screenshot))
            return {"title": "Fake", "content": "body", "screenshot": b"image"}

        async def browser_screenshot(self) -> BrowserScreenshotResponse:
            return {"screenshot": b"saved"}

        async def browser_click(self, _selector: str) -> None:
            raise ValueError("fake click failure")

    async def get_client(_agent_id: uuid.UUID, _environment: str, *, session_id: str) -> Client:
        calls.append((session_id,))
        return Client()

    def store_temp_screenshot(_data: bytes) -> str:
        return "fake-id"

    monkeypatch.setitem(
        sys.modules, "app.services.agentbay_client", SimpleNamespace(get_agentbay_client_for_agent=get_client)
    )
    monkeypatch.setitem(
        sys.modules, "app.services.vision_inject", SimpleNamespace(store_temp_screenshot=store_temp_screenshot)
    )
    navigate_arguments: ToolArguments = {"url": "https://example.test", "wait_for": "#ready", "_session_id": "chat"}
    save_arguments: ToolArguments = {"_session_id": "save"}
    failed_arguments: ToolArguments = {"selector": "#bad"}
    navigate = await agentbay_browser._agentbay_browser_navigate(uuid.uuid4(), tmp_path, navigate_arguments)
    saved = await agentbay_browser._agentbay_browser_save_screenshot(uuid.uuid4(), tmp_path, save_arguments)
    failed = await agentbay_browser._agentbay_browser_click(uuid.uuid4(), tmp_path, failed_arguments)

    assert calls[:2] == [("chat",), ("https://example.test", "#ready", True)]
    assert "[ImageID: fake-id]" in navigate
    assert navigate_arguments == {"url": "https://example.test", "wait_for": "#ready"}
    assert "Screenshot saved to `workspace/screenshots/browser-screenshot-" in saved
    assert next((tmp_path / "workspace" / "screenshots").iterdir()).read_bytes() == b"saved"
    assert failed == "❌ Click failed: fake click failure"


@pytest.mark.asyncio
async def test_code_execute_read_edit_and_command_use_fake_client(monkeypatch, tmp_path: Path) -> None:
    calls: list[CodeCall] = []

    class Filesystem:
        def write_file(self, path: str, content: str, mode: str) -> SimpleNamespace:
            calls.append(("write", path, content, mode))
            return SimpleNamespace(success=True, error_message="")

        def read_file(self, path: str) -> SimpleNamespace:
            calls.append(("read", path))
            return SimpleNamespace(success=True, content="contents", error_message="")

        def edit_file(self, path: str, edits: list[FileEdit], dry_run: bool) -> SimpleNamespace:
            calls.append(("edit", path, edits, dry_run))
            return SimpleNamespace(success=True, error_message="")

    class Client:
        _session = SimpleNamespace(file_system=Filesystem())

        async def code_execute(self, language: str, code: str, execution_timeout: int) -> CodeExecuteResponse:
            calls.append(("execute", language, code, execution_timeout))
            return {"stdout": "ok", "exit_code": 0}

        async def command_exec(self, command: str, *, timeout_ms: int, cwd: str) -> CommandExecResponse:
            calls.append(("command", command, timeout_ms, cwd))
            return {"success": True, "exit_code": 0, "stdout": "listed"}

    async def get_client(_agent_id: uuid.UUID, _environment: str, *, session_id: str) -> Client:
        return Client()

    monkeypatch.setitem(
        sys.modules, "app.services.agentbay_client", SimpleNamespace(get_agentbay_client_for_agent=get_client)
    )
    agent_id = uuid.uuid4()
    execute_arguments: ToolArguments = {"language": "python", "code": "print(1)"}
    read_arguments: ToolArguments = {"remote_path": "/a"}
    edit_arguments: ToolArguments = {"remote_path": "/a", "edits": [{"oldText": "a", "newText": "b"}]}
    command_arguments: ToolArguments = {"command": "ls", "cwd": "/work"}
    execute = await agentbay_code._agentbay_code_execute(agent_id, tmp_path, execute_arguments)
    read = await agentbay_code._agentbay_code_read_file(agent_id, tmp_path, read_arguments)
    edit = await agentbay_code._agentbay_code_edit_file(agent_id, tmp_path, edit_arguments)
    command = await agentbay_code._agentbay_command_exec(agent_id, tmp_path, command_arguments)

    assert "Code execution complete (python)" in execute
    assert read.endswith("contents")
    assert edit == "Edited AgentBay Code Sandbox file: /a (1 replacement(s))"
    assert "Command executed successfully" in command
    assert "stdout:\nlisted" in command
    assert [call[0] for call in calls] == ["execute", "read", "edit", "command"]
