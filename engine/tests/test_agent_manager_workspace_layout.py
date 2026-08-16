"""Workspace layout for OpenClaw guest start."""

from __future__ import annotations

import uuid

import pytest

from app.records.agent import AgentRecord
from app.services.agent_manager import AgentManager


@pytest.mark.asyncio
async def test_initialize_agent_files_runs_after_gateway_key_file(monkeypatch, tmp_path) -> None:
    from app.services.storage_runtime.local import LocalStorageBackend

    storage = LocalStorageBackend(str(tmp_path))
    monkeypatch.setattr("app.services.agent_manager.get_storage_backend", lambda: storage)
    monkeypatch.setattr("app.services.agent_manager.settings.STORAGE_LOCAL_ROOT", str(tmp_path))
    monkeypatch.setattr("app.services.agent_manager.settings.AGENT_TEMPLATE_DIR", str(tmp_path / "missing-template"))

    manager = AgentManager()
    agent_id = uuid.uuid4()
    agent_dir = tmp_path / str(agent_id)
    agent_dir.mkdir()
    (agent_dir / ".maraclaw-gateway-key").write_text("oc-test", encoding="utf-8")

    agent = AgentRecord(
        id=agent_id,
        name="Bot",
        role_description="assistant",
        creator_id=uuid.uuid4(),
    )

    async def no_creator(_creator_id):
        return None

    from app.dao.user_dao import user_dao

    monkeypatch.setattr(user_dao, "get", no_creator)
    await manager.initialize_agent_files(agent=agent)

    assert (agent_dir / "workspace").is_dir() or await storage.exists(f"{agent_id}/workspace/.gitkeep")
    assert await storage.exists(f"{agent_id}/soul.md")


@pytest.mark.asyncio
async def test_materialize_tolerates_dangling_openclaw_workspace_symlink(monkeypatch, tmp_path) -> None:
    from app.services.storage_runtime.local import LocalStorageBackend

    storage = LocalStorageBackend(str(tmp_path))
    monkeypatch.setattr("app.services.agent_manager.get_storage_backend", lambda: storage)
    monkeypatch.setattr("app.services.agent_manager.settings.STORAGE_LOCAL_ROOT", str(tmp_path))

    agent_id = uuid.uuid4()
    agent_dir = tmp_path / str(agent_id)
    openclaw = agent_dir / ".openclaw"
    openclaw.mkdir(parents=True)
    (openclaw / "workspace").symlink_to(agent_dir / "workspace")
    (agent_dir / "soul.md").write_text("# Soul\n", encoding="utf-8")

    manager = AgentManager()
    materialized = await manager._materialize_agent_dir(agent_id)

    assert materialized == agent_dir
    assert (agent_dir / "workspace").is_dir()
    assert (openclaw / "workspace").is_symlink()
