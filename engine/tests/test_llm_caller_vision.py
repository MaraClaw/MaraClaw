import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

from app.services.llm.types import LLMContentPart, LLMMessage, LLMToolCall, ToolPayload
from app.services.vision_inject import VisionContent

AGENT_ID: Final = uuid.UUID("11111111-1111-1111-1111-111111111111")
PLAIN_RESULT: Final = "Screenshot captured."
TOOL_CALL: Final[LLMToolCall] = {
    "id": "call-screenshot",
    "type": "function",
    "function": {
        "name": "agentbay_computer_screenshot",
        "arguments": "{}",
    },
}


@dataclass(frozen=True, slots=True)
class _Settings:
    STORAGE_LOCAL_ROOT: str | None
    AGENT_DATA_DIR: str


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    injected_content: list[VisionContent] | None,
) -> None:
    from app.services import vision_inject
    from app.services.llm import caller

    async def fake_execute_tool(
        tool_name: str,
        arguments: ToolPayload,
        *,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        session_id: str,
        on_output: None,
    ) -> str:
        assert tool_name == "agentbay_computer_screenshot"
        assert arguments == {}
        assert agent_id == AGENT_ID
        assert user_id == AGENT_ID
        assert session_id == "session-1"
        assert on_output is None
        return PLAIN_RESULT

    def fake_get_settings() -> _Settings:
        return _Settings(STORAGE_LOCAL_ROOT=None, AGENT_DATA_DIR="agent-data")

    def fake_inject_vision(
        tool_name: str,
        result_text: str,
        workspace_path: Path,
    ) -> list[VisionContent] | None:
        assert tool_name == "agentbay_computer_screenshot"
        assert result_text == PLAIN_RESULT
        assert workspace_path == Path("agent-data") / str(AGENT_ID)
        return injected_content

    monkeypatch.setattr(caller, "execute_tool", fake_execute_tool)
    monkeypatch.setattr(caller, "get_settings", fake_get_settings)
    monkeypatch.setattr(vision_inject, "try_inject_screenshot_vision", fake_inject_vision)


@pytest.mark.asyncio
async def test_process_tool_call_rebuilds_injected_vision_content_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.llm import caller

    injected_content: list[VisionContent] = [
        {"type": "text", "text": "Screenshot captured."},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
    ]
    expected_content: list[LLMContentPart] = [
        {"type": "text", "text": "Screenshot captured."},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
    ]
    _install_fakes(monkeypatch, injected_content)
    messages: list[LLMMessage] = []

    tool_error = await caller._process_tool_call(
        tc=TOOL_CALL,
        api_messages=messages,
        agent_id=AGENT_ID,
        user_id=None,
        session_id="session-1",
        supports_vision=True,
        on_tool_call=None,
        full_reasoning_content="",
        allowed_tool_names=set(),
    )

    assert tool_error == ""
    assert len(messages) == 1
    assert messages[0].role == "tool"
    assert messages[0].tool_call_id == "call-screenshot"
    assert messages[0].content == expected_content
    rebuilt_content = messages[0].content
    assert isinstance(rebuilt_content, list)
    assert rebuilt_content is not injected_content
    rebuilt_text_part = rebuilt_content[0]
    rebuilt_image_part = rebuilt_content[1]
    injected_text_part = injected_content[0]
    injected_image_part = injected_content[1]
    assert rebuilt_text_part is not injected_text_part
    assert rebuilt_image_part is not injected_image_part
    assert "image_url" in rebuilt_image_part
    assert injected_image_part["type"] == "image_url"
    assert rebuilt_image_part["image_url"] is not injected_image_part["image_url"]


@pytest.mark.asyncio
@pytest.mark.parametrize("injected_content", [None, []], ids=["none", "empty"])
async def test_process_tool_call_keeps_plain_result_when_vision_injection_is_absent(
    monkeypatch: pytest.MonkeyPatch,
    injected_content: list[VisionContent] | None,
) -> None:
    from app.services.llm import caller

    _install_fakes(monkeypatch, injected_content)
    messages: list[LLMMessage] = []

    tool_error = await caller._process_tool_call(
        tc=TOOL_CALL,
        api_messages=messages,
        agent_id=AGENT_ID,
        user_id=None,
        session_id="session-1",
        supports_vision=True,
        on_tool_call=None,
        full_reasoning_content="",
        allowed_tool_names=set(),
    )

    assert tool_error == ""
    assert len(messages) == 1
    assert messages[0].content == PLAIN_RESULT
