"""Inbox skill and wake URL helpers for OpenClaw guests."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.services.openclaw_inbox import (
    guest_engine_base_url,
    openclaw_container_name,
    wake_openclaw_inbox,
    wake_urls,
    write_maraclaw_sync_skill,
)


def test_guest_engine_base_url_is_docker_alias() -> None:
    assert guest_engine_base_url() == "http://maraclaw-engine:8000"


def test_write_skill_uses_engine_alias(tmp_path) -> None:
    path = write_maraclaw_sync_skill(tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "http://maraclaw-engine:8000/api/gateway/poll" in text
    assert "X-Api-Key" in text
    assert "try.maraclaw.ai" not in text


def test_wake_urls_prefer_container_name() -> None:
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id, container_port=19876)
    urls = wake_urls(agent)
    assert urls[0] == f"http://{openclaw_container_name(agent_id)}:18789/hooks/agent"
    assert urls[1] == "http://host.docker.internal:19876/hooks/agent"
    assert not any(url.startswith("http://127.0.0.1:") for url in urls)


async def test_wake_uses_docker_exec_on_loopback(tmp_path, monkeypatch) -> None:
    agent_id = uuid.uuid4()
    agent_dir = tmp_path / str(agent_id)
    agent_dir.mkdir()
    agent = SimpleNamespace(id=agent_id, container_id="container-abc", container_port=19876)
    execute_calls: list[object] = []

    class FakeDocker:
        def __init__(self) -> None:
            self.container = self

        def execute(self, container, argv, stream=False):
            execute_calls.append((container, list(argv), stream))
            return ""

    monkeypatch.setattr("app.services.agent_manager.agent_manager._agent_dir", lambda _id: agent_dir)
    monkeypatch.setattr("app.services.agent_manager.agent_manager.docker_client", FakeDocker())

    woke = await wake_openclaw_inbox(agent, content="Good morning!")
    assert woke is True
    assert execute_calls
    assert execute_calls[0][0] == "container-abc"
    assert execute_calls[0][1][0] == "node"
    payload = (agent_dir / ".maraclaw-wake.json").read_text(encoding="utf-8")
    assert "Good morning!" in payload
