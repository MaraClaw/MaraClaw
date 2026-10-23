"""AgentManager.start_container image pinning and non-rehydrating starts used by OpenClaw upgrades."""

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from python_on_whales.exceptions import NoSuchContainer

from app.services import agent_manager as agent_manager_module
from app.services.agent_manager import AgentManager


class _Containers:
    def inspect(self, reference: str) -> None:
        raise NoSuchContainer(["docker", "inspect", reference], 1)

    def stop(self, *_args: object, **_kwargs: object) -> None:
        return None

    def remove(self, *_args: object, **_kwargs: object) -> None:
        return None


class _Docker:
    def __init__(self) -> None:
        self.container = _Containers()
        self.images: list[str] = []

    def run(self, image: str, *_args: object, **_kwargs: object) -> SimpleNamespace:
        self.images.append(image)
        return SimpleNamespace(id="new-container-id")


class _Env:
    def __init__(self, tmp_path, monkeypatch: pytest.MonkeyPatch, **agent_fields: Any) -> None:
        self.agent_id = uuid.uuid4()
        self.agent_dir = tmp_path / str(self.agent_id)
        self.agent_dir.mkdir()
        self.manager = AgentManager.__new__(AgentManager)
        self.docker = _Docker()
        self.materialized: list[uuid.UUID] = []
        self.restored: list[uuid.UUID] = []
        monkeypatch.setattr(self.manager, "docker_client", self.docker, raising=False)
        monkeypatch.setattr(self.manager, "_agent_dir", lambda _id: self.agent_dir)
        monkeypatch.setattr(agent_manager_module.settings, "STORAGE_LOCAL_ROOT", str(tmp_path))
        monkeypatch.setattr(agent_manager_module.settings, "GOGCLI_ENABLED", True)

        async def materialize(agent_id: uuid.UUID):
            self.materialized.append(agent_id)
            return self.agent_dir

        async def restore(agent_id: uuid.UUID, _agent_dir):
            self.restored.append(agent_id)
            return True

        monkeypatch.setattr(self.manager, "_materialize_agent_dir", materialize)
        monkeypatch.setattr(agent_manager_module, "restore_gogcli_state", restore)
        self.agent: Any = SimpleNamespace(
            id=self.agent_id,
            name="Pinned",
            creator_id=uuid.uuid4(),
            primary_model_id=None,
            gogcli_enabled=True,
            setup_pending=False,
            container_id=None,
            container_port=None,
            **agent_fields,
        )


async def test_ordinary_start_uses_pinned_image_then_configured_image(tmp_path, monkeypatch):
    # Given a pinned agent and an unpinned one.
    pinned = _Env(tmp_path, monkeypatch, openclaw_image_id="sha256:pinned")
    await pinned.manager.start_container(pinned.agent)
    unpinned = _Env(tmp_path, monkeypatch, openclaw_image_id=None)
    await unpinned.manager.start_container(unpinned.agent)
    # Then the pin wins over the configured tag; no pin falls back to configuration.
    assert pinned.docker.images == ["sha256:pinned"]
    assert unpinned.docker.images == [agent_manager_module.settings.MARACLAW_OPENCLAW_IMAGE]


async def test_explicit_image_overrides_the_pin(tmp_path, monkeypatch):
    env = _Env(tmp_path, monkeypatch, openclaw_image_id="sha256:old")
    await env.manager.start_container(env.agent, image="sha256:target")
    assert env.docker.images == ["sha256:target"]


async def test_rehydrate_false_skips_storage_and_gogcli_and_keeps_guest_resources(tmp_path, monkeypatch):
    # Given on-disk guest resources the engine does not manage.
    env = _Env(tmp_path, monkeypatch, openclaw_image_id=None)
    (env.agent_dir / "openclaw.json").write_text(
        json.dumps(
            {
                "mcp": {"servers": {"docs": {"url": "https://mcp.invalid"}}},
                "skills": {"entries": {"mine": {"enabled": True}}, "load": {"extraDirs": ["/skills"]}},
                "plugins": {"entries": {"custom": {"enabled": True}}},
            }
        ),
        encoding="utf-8",
    )
    # When the upgrade executor starts the replacement without rehydrating.
    container_id = await env.manager.start_container(env.agent, image="sha256:target", rehydrate=False)
    # Then nothing was copied from storage or gogcli, and the guest resources survive the config rewrite.
    assert container_id == "new-container-id"
    assert env.materialized == []
    assert env.restored == []
    config = json.loads((env.agent_dir / "openclaw.json").read_text(encoding="utf-8"))
    assert config["mcp"]["servers"]["docs"] == {"url": "https://mcp.invalid"}
    assert config["skills"]["entries"]["mine"] == {"enabled": True}
    assert config["skills"]["load"]["extraDirs"] == ["/skills"]
    assert config["plugins"]["entries"]["custom"] == {"enabled": True}


async def test_default_start_still_rehydrates(tmp_path, monkeypatch):
    env = _Env(tmp_path, monkeypatch, openclaw_image_id=None)
    await env.manager.start_container(env.agent)
    assert env.materialized == [env.agent_id]
    assert env.restored == [env.agent_id]
