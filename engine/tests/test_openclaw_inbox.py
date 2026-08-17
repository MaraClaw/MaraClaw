"""Inbox skill and wake URL helpers for OpenClaw guests."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.services import openclaw_inbox
from app.services.openclaw_inbox import (
    guest_engine_base_url,
    inbox_cli_argv,
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
    bootstrap = tmp_path / "workspace" / "BOOTSTRAP.md"
    assert bootstrap.is_file()
    assert "maraclaw-sync" in bootstrap.read_text(encoding="utf-8")


def test_wake_urls_prefer_container_name() -> None:
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id, container_port=19876)
    urls = wake_urls(agent)
    assert urls[0] == f"http://{openclaw_container_name(agent_id)}:18789/hooks/agent"
    assert urls[1] == "http://host.docker.internal:19876/hooks/agent"
    assert not any(url.startswith("http://127.0.0.1:") for url in urls)


async def test_wake_uses_http_hooks_when_reachable(tmp_path, monkeypatch) -> None:
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

    async def http_ok(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.services.agent_manager.agent_manager._agent_dir", lambda _id: agent_dir)
    monkeypatch.setattr("app.services.agent_manager.agent_manager.docker_client", FakeDocker())
    monkeypatch.setattr(openclaw_inbox, "_http_hooks_wake", http_ok)

    woke = await wake_openclaw_inbox(agent, content="Good morning!", message_id=agent_id)
    assert woke is True
    assert execute_calls == []


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

    async def http_down(*_args, **_kwargs):
        return "http hooks unreachable in test"

    monkeypatch.setattr(openclaw_inbox, "_HOOK_RETRY_ATTEMPTS", 1)
    monkeypatch.setattr(openclaw_inbox, "_HOOK_RETRY_SECONDS", 0)
    monkeypatch.setattr("app.services.agent_manager.agent_manager._agent_dir", lambda _id: agent_dir)
    monkeypatch.setattr("app.services.agent_manager.agent_manager.docker_client", FakeDocker())
    monkeypatch.setattr(openclaw_inbox, "_http_hooks_wake", http_down)

    woke = await wake_openclaw_inbox(agent, content="Good morning!")
    assert woke is True
    assert execute_calls
    assert execute_calls[0][0] == "container-abc"
    assert execute_calls[0][1][0] == "node"
    payload = (agent_dir / ".maraclaw-wake.json").read_text(encoding="utf-8")
    assert "Good morning!" in payload


def test_inbox_cli_targets_default_agent_locally() -> None:
    argv = inbox_cli_argv("Hello again.")
    assert argv[:6] == ["openclaw", "agent", "--agent", "main", "--local", "--message"]
    assert argv[6] == "Hello again."


async def test_wake_leaves_pending_when_hooks_refuse(tmp_path, monkeypatch) -> None:
    agent_id = uuid.uuid4()
    agent_dir = tmp_path / str(agent_id)
    agent_dir.mkdir()
    agent = SimpleNamespace(id=agent_id, container_id="container-abc", container_port=19876)
    execute_calls: list[list[str]] = []

    class FakeDocker:
        def __init__(self) -> None:
            self.container = self

        def execute(self, container, argv, stream=False):
            execute_calls.append(list(argv))
            raise RuntimeError("ECONNREFUSED 18789")

    async def http_down(*_args, **_kwargs):
        return "ECONNREFUSED 18789"

    async def no_recreate(*_args, **_kwargs):
        return None

    monkeypatch.setattr(openclaw_inbox, "_HOOK_RETRY_ATTEMPTS", 1)
    monkeypatch.setattr(openclaw_inbox, "_HOOK_RETRY_SECONDS", 0)
    monkeypatch.setattr(openclaw_inbox, "_GATEWAY_WAIT_ATTEMPTS", 1)
    monkeypatch.setattr(openclaw_inbox, "_GATEWAY_WAIT_SECONDS", 0)
    monkeypatch.setattr("app.services.agent_manager.agent_manager._agent_dir", lambda _id: agent_dir)
    monkeypatch.setattr("app.services.agent_manager.agent_manager.docker_client", FakeDocker())
    monkeypatch.setattr("app.services.agent_manager.agent_manager.start_container", no_recreate)
    monkeypatch.setattr(openclaw_inbox, "_http_hooks_wake", http_down)

    woke = await wake_openclaw_inbox(agent, content="Hello.")
    assert woke is False
    assert execute_calls[0][0] == "node"


async def test_wake_starts_guest_gateway_then_hooks(tmp_path, monkeypatch) -> None:
    agent_id = uuid.uuid4()
    agent_dir = tmp_path / str(agent_id)
    agent_dir.mkdir()
    agent = SimpleNamespace(id=agent_id, container_id="container-abc", container_port=19876)
    execute_calls: list[list[str]] = []
    started = False

    class FakeDocker:
        def __init__(self) -> None:
            self.container = self

        def execute(self, container, argv, stream=False):
            execute_calls.append(list(argv))
            joined = " ".join(str(part) for part in argv)
            nonlocal started
            if "nohup openclaw gateway" in joined:
                started = True
                return ""
            if "18789" in joined and "fetch" in joined:
                if started:
                    return ""
                raise RuntimeError("ECONNREFUSED 18789")
            if argv and argv[0] == "node":
                if started:
                    return ""
                raise RuntimeError("ECONNREFUSED 18789")
            return ""

    async def http_down(*_args, **_kwargs):
        return "ECONNREFUSED 18789"

    async def no_recreate(*_args, **_kwargs):
        return None

    monkeypatch.setattr(openclaw_inbox, "_HOOK_RETRY_ATTEMPTS", 1)
    monkeypatch.setattr(openclaw_inbox, "_HOOK_RETRY_SECONDS", 0)
    monkeypatch.setattr(openclaw_inbox, "_GATEWAY_WAIT_ATTEMPTS", 2)
    monkeypatch.setattr(openclaw_inbox, "_GATEWAY_WAIT_SECONDS", 0)
    monkeypatch.setattr("app.services.agent_manager.agent_manager._agent_dir", lambda _id: agent_dir)
    monkeypatch.setattr("app.services.agent_manager.agent_manager.docker_client", FakeDocker())
    monkeypatch.setattr("app.services.agent_manager.agent_manager.start_container", no_recreate)
    monkeypatch.setattr(openclaw_inbox, "_http_hooks_wake", http_down)

    woke = await wake_openclaw_inbox(agent, content="Hello.")
    assert woke is True
    assert started is True
    assert any(call[0] == "node" for call in execute_calls)


async def test_wake_skips_in_guest_hooks_after_recent_econnrefused(tmp_path, monkeypatch) -> None:
    agent_id = uuid.uuid4()
    agent_dir = tmp_path / str(agent_id)
    agent_dir.mkdir()
    agent = SimpleNamespace(id=agent_id, container_id="container-abc", container_port=19876)
    execute_calls: list[list[str]] = []

    class FakeDocker:
        def __init__(self) -> None:
            self.container = self

        def execute(self, container, argv, stream=False):
            execute_calls.append(list(argv))
            return ""

    async def http_down(*_args, **_kwargs):
        return "http hooks unreachable in test"

    monkeypatch.setattr("app.services.agent_manager.agent_manager._agent_dir", lambda _id: agent_dir)
    monkeypatch.setattr("app.services.agent_manager.agent_manager.docker_client", FakeDocker())
    monkeypatch.setattr(openclaw_inbox, "_http_hooks_wake", http_down)
    openclaw_inbox._HOOKS_DOWN_UNTIL[agent_id] = openclaw_inbox.time.monotonic() + 30

    woke = await wake_openclaw_inbox(agent, content="Hello.")
    assert woke is False
    assert execute_calls == []
    openclaw_inbox._HOOKS_DOWN_UNTIL.pop(agent_id, None)


async def test_http_refuse_still_tries_in_guest_hooks(tmp_path, monkeypatch) -> None:
    agent_id = uuid.uuid4()
    agent_dir = tmp_path / str(agent_id)
    agent_dir.mkdir()
    agent = SimpleNamespace(id=agent_id, container_id="container-abc", container_port=19876)
    execute_calls: list[list[str]] = []

    class FakeDocker:
        def __init__(self) -> None:
            self.container = self

        def execute(self, container, argv, stream=False):
            execute_calls.append(list(argv))
            return ""

    async def http_down(*_args, **_kwargs):
        return "ECONNREFUSED 18789"

    monkeypatch.setattr(openclaw_inbox, "_HOOK_RETRY_ATTEMPTS", 1)
    monkeypatch.setattr(openclaw_inbox, "_HOOK_RETRY_SECONDS", 0)
    monkeypatch.setattr("app.services.agent_manager.agent_manager._agent_dir", lambda _id: agent_dir)
    monkeypatch.setattr("app.services.agent_manager.agent_manager.docker_client", FakeDocker())
    monkeypatch.setattr(openclaw_inbox, "_http_hooks_wake", http_down)

    woke = await wake_openclaw_inbox(agent, content="Hello.")
    assert woke is True
    assert execute_calls[0][0] == "node"
    assert all(call[0] != "openclaw" for call in execute_calls)


async def test_skips_cli_when_inbox_already_reported(tmp_path, monkeypatch) -> None:
    agent_id = uuid.uuid4()
    message_id = uuid.uuid4()
    agent_dir = tmp_path / str(agent_id)
    agent_dir.mkdir()
    agent = SimpleNamespace(id=agent_id, container_id="container-abc", container_port=19876)
    execute_calls: list[list[str]] = []

    class FakeDocker:
        def __init__(self) -> None:
            self.container = self

        def execute(self, container, argv, stream=False):
            execute_calls.append(list(argv))
            return ""

    async def http_down(*_args, **_kwargs):
        return "ECONNREFUSED 18789"

    async def already_done(_message_id, _agent_id):
        return SimpleNamespace(status="completed")

    from app.dao.gateway_message_dao import gateway_message_dao

    monkeypatch.setattr("app.services.agent_manager.agent_manager._agent_dir", lambda _id: agent_dir)
    monkeypatch.setattr("app.services.agent_manager.agent_manager.docker_client", FakeDocker())
    monkeypatch.setattr(openclaw_inbox, "_http_hooks_wake", http_down)
    monkeypatch.setattr(gateway_message_dao, "get_for_agent", already_done)

    woke = await wake_openclaw_inbox(agent, content="Hello.", message_id=message_id)
    assert woke is True
    assert execute_calls == []


def test_wake_body_includes_message_id_for_direct_report() -> None:
    message_id = uuid.uuid4()
    body = openclaw_inbox._wake_body("Please introduce yourself.", message_id=message_id)
    assert str(message_id) in body["message"]
    assert "/api/gateway/report" in body["message"]
    assert "Please introduce yourself." in body["message"]
