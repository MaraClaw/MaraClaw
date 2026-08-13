from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.agent_tool_exec import agentbay_files


@pytest.mark.asyncio
async def test_environment_transfer_uses_temporary_file_and_cleans_it(monkeypatch, tmp_path: Path) -> None:
    temporary_paths: list[str] = []

    class Filesystem:
        def download_file(self, _remote: str, local: str) -> SimpleNamespace:
            temporary_paths.append(local)
            Path(local).write_text("fake")
            return SimpleNamespace(success=True, bytes_received=4, error_message="")

        def upload_file(self, local: str, _remote: str) -> SimpleNamespace:
            assert Path(local).read_text() == "fake"
            return SimpleNamespace(success=True, bytes_sent=4, error_message="")

    class Client:
        _session = SimpleNamespace(file_system=Filesystem())

    async def get_client(*_args: object, **_kwargs: object) -> Client:
        return Client()

    monkeypatch.setitem(
        sys.modules, "app.services.agentbay_client", SimpleNamespace(get_agentbay_client_for_agent=get_client)
    )
    result = await agentbay_files._agentbay_file_transfer(
        uuid.uuid4(),
        tmp_path,
        {
            "from_type": "browser",
            "from_path": "/source",
            "to_type": "code",
            "to_path": "/target",
            "_session_id": "chat",
        },
    )

    assert result == "Transferred [browser]/source → [code]/target (4 bytes)"
    assert len(temporary_paths) == 1
    assert not (await asyncio.to_thread(Path(temporary_paths[0]).exists))


@pytest.mark.asyncio
async def test_workspace_environment_and_invalid_transfer_paths_use_fake_clients(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[object, ...]] = []

    class Filesystem:
        def upload_file(self, local: str, remote: str) -> SimpleNamespace:
            calls.append(("upload", Path(local).name, remote))
            return SimpleNamespace(success=True, bytes_sent=3, error_message="")

        def download_file(self, remote: str, local: str) -> SimpleNamespace:
            calls.append(("download", remote, Path(local).name))
            Path(local).write_text("downloaded")
            return SimpleNamespace(success=True, bytes_received=10, error_message="")

    class Command:
        def exec(self, _command: str) -> None:
            raise OSError("refresh unavailable")

    class Client:
        _session = SimpleNamespace(file_system=Filesystem(), command=Command())

    async def get_client(*_args: object, **_kwargs: object) -> Client:
        return Client()

    monkeypatch.setitem(
        sys.modules, "app.services.agentbay_client", SimpleNamespace(get_agentbay_client_for_agent=get_client)
    )
    (tmp_path / "report.txt").write_text("abc")
    agent_id = uuid.uuid4()
    upload = await agentbay_files._agentbay_file_transfer(
        agent_id,
        tmp_path,
        {
            "from_type": "workspace",
            "from_path": "report.txt",
            "to_type": "computer",
            "to_path": "/home/wuying/桌面/report.txt",
        },
    )
    download = await agentbay_files._agentbay_file_transfer(
        agent_id,
        tmp_path,
        {"from_type": "code", "from_path": "/remote", "to_type": "workspace", "to_path": "nested/out.txt"},
    )
    rejected_path = await agentbay_files._agentbay_file_transfer(
        agent_id,
        tmp_path,
        {"from_type": "workspace", "from_path": "../outside", "to_type": "code", "to_path": "/remote"},
    )
    rejected_endpoint = await agentbay_files._agentbay_file_transfer(
        agent_id,
        tmp_path,
        {"from_type": "unknown", "from_path": "/a", "to_type": "code", "to_path": "/b"},
    )

    assert upload == "Transferred workspace/report.txt → [computer]/home/wuying/桌面/report.txt (3 bytes)"
    assert download.endswith("File available in workspace at: nested/out.txt")
    assert (tmp_path / "nested" / "out.txt").read_text() == "downloaded"
    assert rejected_path == "Permission denied: path must be inside the agent workspace"
    assert rejected_endpoint == "Unsupported transfer: unknown → code"
    assert calls == [("upload", "report.txt", "/home/wuying/桌面/report.txt"), ("download", "/remote", "out.txt")]


@pytest.mark.asyncio
async def test_failed_environment_bridge_still_cleans_temporary_file(monkeypatch, tmp_path: Path) -> None:
    temporary_paths: list[str] = []

    class Filesystem:
        def download_file(self, _remote: str, local: str) -> SimpleNamespace:
            temporary_paths.append(local)
            Path(local).write_text("partial")
            return SimpleNamespace(success=False, bytes_received=0, error_message="download failed")

    class Client:
        _session = SimpleNamespace(file_system=Filesystem())

    async def get_client(*_args: object, **_kwargs: object) -> Client:
        return Client()

    monkeypatch.setitem(
        sys.modules, "app.services.agentbay_client", SimpleNamespace(get_agentbay_client_for_agent=get_client)
    )
    result = await agentbay_files._agentbay_file_transfer(
        uuid.uuid4(), tmp_path, {"from_type": "browser", "from_path": "/a", "to_type": "code", "to_path": "/b"}
    )

    assert result == "Transfer failed (download from browser): download failed"
    assert len(temporary_paths) == 1
    assert not (await asyncio.to_thread(Path(temporary_paths[0]).exists))
