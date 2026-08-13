from __future__ import annotations

import importlib
import uuid
from pathlib import Path

from app.services.agent_tool_exec import channel_context
from app.services.channel_session import find_or_create_channel_session
from app.services.channel_user_service import get_platform_user_by_org_member


def test_channel_contextvars_are_exported_from_channel_context_and_apis() -> None:
    context = importlib.import_module("app.services.agent_tool_exec.channel_context")

    assert channel_context.channel_file_sender is context.channel_file_sender
    assert channel_context.channel_web_agent_id is context.channel_web_agent_id
    assert channel_context.channel_feishu_sender_open_id is context.channel_feishu_sender_open_id

    teams_api = importlib.import_module("app.api.teams")
    assert teams_api._cfs_s is context.channel_file_sender


def test_channel_runtime_dependencies_import_from_owners() -> None:
    assert find_or_create_channel_session is not None
    assert get_platform_user_by_org_member is not None


async def test_send_channel_file_uses_sender_contextvar(tmp_path: Path) -> None:
    channel_files = importlib.import_module("app.services.agent_tool_exec.channel_files")
    agent_id = uuid.uuid4()
    source_file = tmp_path / "report.txt"
    source_file.write_text("daily report", encoding="utf-8")
    calls: list[tuple[Path, str]] = []

    async def sender(file_path: Path, message: str = "") -> None:
        calls.append((file_path, message))

    token = channel_context.channel_file_sender.set(sender)
    try:
        result = await channel_files._send_channel_file(
            agent_id,
            tmp_path,
            {"file_path": "report.txt", "message": "please review"},
        )
    finally:
        channel_context.channel_file_sender.reset(token)

    assert result == "File 'report.txt' sent to user via channel."
    assert calls == [(source_file.resolve(), "please review")]


async def test_send_channel_file_missing_sender_keeps_download_and_error_behavior(tmp_path: Path) -> None:
    channel_files = importlib.import_module("app.services.agent_tool_exec.channel_files")
    agent_id = uuid.uuid4()
    source_file = tmp_path / "report.txt"
    source_file.write_text("daily report", encoding="utf-8")

    token = channel_context.channel_file_sender.set(None)
    try:
        result = await channel_files._send_channel_file(
            agent_id,
            tmp_path,
            {"file_path": "report.txt"},
        )
    finally:
        channel_context.channel_file_sender.reset(token)

    assert "no active channel sender" in result.lower() or "download" in result.lower() or "❌" in result


def test_okr_daily_collection_imports_channel_message_from_owner() -> None:
    from app.services import okr_daily_collection
    from app.services.agent_tool_exec.channel_messaging import _send_channel_message

    assert okr_daily_collection._send_channel_message is _send_channel_message
