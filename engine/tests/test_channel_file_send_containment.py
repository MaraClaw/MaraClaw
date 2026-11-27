from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.services import agent_tools
from app.services.agent_tool_exec import channel_files

DENIED = "Access denied for this path"


@pytest.mark.parametrize("via", ["parent_traversal", "symlinked_directory", "symlinked_file"])
async def test_channel_send_rejects_prefix_sibling_of_workspace(tmp_path: Path, via: str):
    agent_id = uuid.uuid4()
    workspace = tmp_path / str(agent_id)
    sibling = tmp_path / f"{agent_id}-other"
    workspace.mkdir()
    sibling.mkdir()
    (sibling / "secret.txt").write_text("secret")
    (workspace / "dir-link").symlink_to(sibling, target_is_directory=True)
    (workspace / "file-link.txt").symlink_to(sibling / "secret.txt")
    rel_path = {
        "parent_traversal": f"../{agent_id}-other/secret.txt",
        "symlinked_directory": "dir-link/secret.txt",
        "symlinked_file": "file-link.txt",
    }[via]

    result = await channel_files._send_channel_file(agent_id, workspace, {"file_path": rel_path})

    assert result == DENIED


async def test_channel_send_does_not_retry_an_unchecked_workspace_root_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    agent_id = uuid.uuid4()
    workspace_root = tmp_path / "root"
    other_agent_file = workspace_root / "x" / "real.txt"
    other_agent_file.parent.mkdir(parents=True)
    other_agent_file.write_text("another agent's file")
    temp_workspace = tmp_path / "temp" / "ws"
    temp_workspace.mkdir(parents=True)
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", workspace_root)

    result = await channel_files._send_channel_file(agent_id, temp_workspace, {"file_path": "../x/real.txt"})

    assert result == DENIED


async def test_channel_send_still_links_files_reached_through_inside_symlinks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    agent_id = uuid.uuid4()
    workspace = tmp_path / str(agent_id)
    (workspace / "real").mkdir(parents=True)
    (workspace / "real" / "report.txt").write_text("report")
    (workspace / "alias").symlink_to(workspace / "real", target_is_directory=True)
    (workspace / "latest.txt").symlink_to(workspace / "real" / "report.txt")
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)

    by_directory = await channel_files._send_channel_file(agent_id, workspace, {"file_path": "alias/report.txt"})
    by_file = await channel_files._send_channel_file(agent_id, workspace, {"file_path": "latest.txt"})

    for result in (by_directory, by_file):
        assert result.startswith("File ready: [report.txt](")
        assert result.endswith(f"/api/agents/{agent_id}/files/download?path=real/report.txt)")
