"""Inbox skill and wake URL helpers for OpenClaw guests."""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pywhales.exceptions import DockerException, NoSuchContainer

from app.services import openclaw_inbox
from app.services.openclaw_inbox import (
    guest_engine_base_url,
    inbox_cli_argv,
    wake_openclaw_inbox,
    write_maraclaw_sync_skill,
)
from guest_bootstrap_fakes import BOOTSTRAP_PENDING_EXIT, GUARD, PENDING_STDERR, guard_refusal


class OwnedGuestInspect:
    def __init__(self, agent_id: uuid.UUID) -> None:
        self.agent_id = agent_id

    def inspect(self, reference: str) -> SimpleNamespace:
        name = f"maraclaw-agent-{self.agent_id}"
        if reference not in ("container-abc", name):
            raise NoSuchContainer(["docker", "inspect", reference], 1)
        return SimpleNamespace(
            id="container-abc",
            name=name,
            config=SimpleNamespace(labels={"maraclaw.agent_id": str(self.agent_id)}),
            host_config=SimpleNamespace(port_bindings={}),
        )


class ScriptedDocker(OwnedGuestInspect):
    """Records every exec; each call consumes the next outcome (the last repeats), reading the payload it names."""

    def __init__(self, agent_id: uuid.UUID, directory: Path, *outcomes: Exception | None) -> None:
        super().__init__(agent_id)
        self.container = self
        self.directory = directory
        self.outcomes = list(outcomes) or [None]
        self.calls: list[list[str]] = []
        self.payloads: list[str] = []

    def execute(self, container, argv, stream=False):
        self.calls.append(list(argv))
        if argv[-1].endswith(".json"):
            self.payloads.append((self.directory / Path(argv[-1]).name).read_text(encoding="utf-8"))
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if outcome is not None:
            raise outcome
        return ""


def forbid_host_http(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        pytest.fail("the engine contacted the guest gateway over host HTTP")

    monkeypatch.setattr(httpx, "AsyncClient", refuse)
    monkeypatch.setattr(httpx, "Client", refuse)


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


async def test_wake_delivers_through_the_guarded_in_guest_hooks(tmp_path, monkeypatch) -> None:
    agent_id = uuid.uuid4()
    agent_dir = tmp_path / str(agent_id)
    agent_dir.mkdir()
    agent: Any = SimpleNamespace(id=agent_id, container_id="container-abc", container_port=19876)
    docker = ScriptedDocker(agent_id, agent_dir)
    monkeypatch.setattr("app.services.agent_manager.agent_manager._agent_dir", lambda _id: agent_dir)
    monkeypatch.setattr("app.services.agent_manager.agent_manager.docker_client", docker)
    forbid_host_http(monkeypatch)

    woke = await wake_openclaw_inbox(agent, content="Good morning!", message_id=agent_id)

    assert woke is True
    assert len(docker.calls) == 1
    assert docker.calls[0][: len(GUARD)] == GUARD
    assert docker.calls[0][len(GUARD)] == "node"
    assert "Good morning!" in docker.payloads[0]
    assert not list(agent_dir.glob(".maraclaw-wake-*.json"))


async def test_each_wake_uses_its_own_payload_file(tmp_path, monkeypatch) -> None:
    agent_id = uuid.uuid4()
    agent_dir = tmp_path / str(agent_id)
    agent_dir.mkdir()
    agent: Any = SimpleNamespace(id=agent_id, container_id="container-abc")
    docker = ScriptedDocker(agent_id, agent_dir)
    monkeypatch.setattr("app.services.agent_manager.agent_manager.docker_client", docker)

    first = openclaw_inbox._exec_in_guest_hooks(agent, agent_dir, openclaw_inbox._wake_body("first"))
    second = openclaw_inbox._exec_in_guest_hooks(agent, agent_dir, openclaw_inbox._wake_body("second"))

    assert first is second is None
    assert docker.calls[0][-1] != docker.calls[1][-1]
    assert ["first" in docker.payloads[0], "second" in docker.payloads[1]] == [True, True]


def test_inbox_cli_targets_default_agent_locally() -> None:
    argv = inbox_cli_argv("Hello again.")
    assert argv[:6] == ["openclaw", "agent", "--agent", "main", "--local", "--message"]
    assert argv[6] == "Hello again."


async def test_wake_leaves_pending_when_hooks_refuse(tmp_path, monkeypatch) -> None:
    agent_id = uuid.uuid4()
    agent_dir = tmp_path / str(agent_id)
    agent_dir.mkdir()
    agent: Any = SimpleNamespace(id=agent_id, container_id="container-abc", container_port=19876)
    docker = ScriptedDocker(agent_id, agent_dir, RuntimeError("ECONNREFUSED 18789"))
    monkeypatch.setattr(openclaw_inbox, "_HOOK_RETRY_ATTEMPTS", 1)
    monkeypatch.setattr(openclaw_inbox, "_HOOK_RETRY_SECONDS", 0)
    monkeypatch.setattr("app.services.agent_manager.agent_manager._agent_dir", lambda _id: agent_dir)
    monkeypatch.setattr("app.services.agent_manager.agent_manager.docker_client", docker)
    forbid_host_http(monkeypatch)

    woke = await wake_openclaw_inbox(agent, content="Hello.")

    assert woke is False
    assert docker.calls[0][: len(GUARD)] == GUARD
    assert docker.calls[0][len(GUARD)] == "node"


async def test_wake_never_starts_the_guest_gateway(tmp_path, monkeypatch) -> None:
    # Given a refusing gateway; the guest's bootstrap helper, not the engine, owns its startup.
    agent_id = uuid.uuid4()
    agent_dir = tmp_path / str(agent_id)
    agent_dir.mkdir()
    agent: Any = SimpleNamespace(id=agent_id, container_id="container-abc", container_port=19876)
    docker = ScriptedDocker(agent_id, agent_dir, DockerException(["docker", "exec"], 1, stderr=b"ECONNREFUSED 18789"))
    monkeypatch.setattr(openclaw_inbox, "_HOOK_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr(openclaw_inbox, "_HOOK_RETRY_SECONDS", 0)
    monkeypatch.setattr("app.services.agent_manager.agent_manager._agent_dir", lambda _id: agent_dir)
    monkeypatch.setattr("app.services.agent_manager.agent_manager.docker_client", docker)
    forbid_host_http(monkeypatch)

    woke = await wake_openclaw_inbox(agent, content="Hello.")

    assert woke is False
    assert len(docker.calls) == 2
    assert all(call[: len(GUARD)] == GUARD and call[len(GUARD)] == "node" for call in docker.calls)


@pytest.mark.parametrize("attempts", [1, 2])
async def test_pending_bootstrap_leaves_work_pending_without_any_other_transport(
    tmp_path, monkeypatch, attempts
) -> None:
    # Given the guest's current boot has not finished bootstrapping.
    agent_id = uuid.uuid4()
    agent_dir = tmp_path / str(agent_id)
    agent_dir.mkdir()
    agent: Any = SimpleNamespace(id=agent_id, container_id="container-abc", container_port=19876)
    docker = ScriptedDocker(agent_id, agent_dir, guard_refusal())
    monkeypatch.setattr(openclaw_inbox, "_HOOK_RETRY_ATTEMPTS", attempts)
    monkeypatch.setattr(openclaw_inbox, "_HOOK_RETRY_SECONDS", 0)
    monkeypatch.setattr("app.services.agent_manager.agent_manager._agent_dir", lambda _id: agent_dir)
    monkeypatch.setattr("app.services.agent_manager.agent_manager.docker_client", docker)
    forbid_host_http(monkeypatch)

    woke = await wake_openclaw_inbox(agent, content="Hello.")

    # Then the one guarded attempt is final: no retry, host HTTP, CLI, or gateway start.
    assert woke is False
    assert len(docker.calls) == 1
    assert docker.calls[0][: len(GUARD)] == GUARD
    assert agent_id not in openclaw_inbox._HOOKS_DOWN_UNTIL


async def test_wake_skips_in_guest_hooks_after_recent_econnrefused(tmp_path, monkeypatch) -> None:
    agent_id = uuid.uuid4()
    agent_dir = tmp_path / str(agent_id)
    agent_dir.mkdir()
    agent: Any = SimpleNamespace(id=agent_id, container_id="container-abc", container_port=19876)
    docker = ScriptedDocker(agent_id, agent_dir)
    monkeypatch.setattr("app.services.agent_manager.agent_manager._agent_dir", lambda _id: agent_dir)
    monkeypatch.setattr("app.services.agent_manager.agent_manager.docker_client", docker)
    forbid_host_http(monkeypatch)
    openclaw_inbox._HOOKS_DOWN_UNTIL[agent_id] = openclaw_inbox.time.monotonic() + 30

    woke = await wake_openclaw_inbox(agent, content="Hello.")
    assert woke is False
    assert docker.calls == []
    openclaw_inbox._HOOKS_DOWN_UNTIL.pop(agent_id, None)


async def test_refused_hooks_are_retried_through_the_guard(tmp_path, monkeypatch) -> None:
    agent_id = uuid.uuid4()
    agent_dir = tmp_path / str(agent_id)
    agent_dir.mkdir()
    agent: Any = SimpleNamespace(id=agent_id, container_id="container-abc", container_port=19876)
    docker = ScriptedDocker(agent_id, agent_dir, RuntimeError("ECONNREFUSED 18789"), None)
    monkeypatch.setattr(openclaw_inbox, "_HOOK_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr(openclaw_inbox, "_HOOK_RETRY_SECONDS", 0)
    monkeypatch.setattr("app.services.agent_manager.agent_manager._agent_dir", lambda _id: agent_dir)
    monkeypatch.setattr("app.services.agent_manager.agent_manager.docker_client", docker)
    forbid_host_http(monkeypatch)

    woke = await wake_openclaw_inbox(agent, content="Hello.")

    assert woke is True
    assert len(docker.calls) == 2
    assert all(call[: len(GUARD)] == GUARD and call[len(GUARD)] == "node" for call in docker.calls)


async def test_skips_redelivery_when_inbox_already_reported(tmp_path, monkeypatch) -> None:
    agent_id = uuid.uuid4()
    agent_dir = tmp_path / str(agent_id)
    agent_dir.mkdir()
    agent: Any = SimpleNamespace(id=agent_id, container_id="container-abc", container_port=19876)
    message_id = uuid.uuid4()
    docker = ScriptedDocker(agent_id, agent_dir, RuntimeError("ECONNREFUSED 18789"))

    async def already_done(_message_id, _agent_id):
        return SimpleNamespace(status="completed")

    from app.dao.gateway_message_dao import gateway_message_dao

    monkeypatch.setattr(openclaw_inbox, "_HOOK_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr(openclaw_inbox, "_HOOK_RETRY_SECONDS", 0)
    monkeypatch.setattr("app.services.agent_manager.agent_manager._agent_dir", lambda _id: agent_dir)
    monkeypatch.setattr("app.services.agent_manager.agent_manager.docker_client", docker)
    forbid_host_http(monkeypatch)
    monkeypatch.setattr(gateway_message_dao, "get_for_agent", already_done)

    woke = await wake_openclaw_inbox(agent, content="Hello.", message_id=message_id)
    assert woke is True
    assert len(docker.calls) == 1


def test_hooks_unreachable_ignores_port_and_http_status() -> None:
    url_401 = "http://maraclaw-agent-deadbeef:18789/hooks/agent: HTTP 401"
    assert openclaw_inbox._hooks_unreachable(url_401) is False
    assert openclaw_inbox._hooks_unreachable("ECONNREFUSED 127.0.0.1:18789") is True
    assert openclaw_inbox._hooks_unreachable("ConnectError: all connection attempts failed") is True


async def test_hook_denial_is_not_retried_and_never_starts_the_gateway(tmp_path, monkeypatch) -> None:
    agent_id = uuid.uuid4()
    agent_dir = tmp_path / str(agent_id)
    agent_dir.mkdir()
    agent: Any = SimpleNamespace(id=agent_id, container_id="container-abc", container_port=19876)
    warnings: list[str] = []
    denial = DockerException(
        ["docker", "exec", "command-secret"], 1, stdout=b"HTTP 401 stdout-secret", stderr=b"ECONNREFUSED stderr-secret"
    )
    docker = ScriptedDocker(agent_id, agent_dir, denial)
    monkeypatch.setattr(openclaw_inbox, "_HOOK_RETRY_ATTEMPTS", 1)
    monkeypatch.setattr(openclaw_inbox, "_HOOK_RETRY_SECONDS", 0)
    monkeypatch.setattr("app.services.agent_manager.agent_manager._agent_dir", lambda _id: agent_dir)
    monkeypatch.setattr("app.services.agent_manager.agent_manager.docker_client", docker)
    forbid_host_http(monkeypatch)
    monkeypatch.setattr(openclaw_inbox.logger, "warning", lambda message, *args: warnings.append(message.format(*args)))

    woke = await wake_openclaw_inbox(agent, content="Hello.")
    assert woke is False
    assert len(docker.calls) == 1
    assert docker.calls[0][: len(GUARD)] == GUARD
    assert len(warnings) == 1
    assert str(denial) in warnings[0]
    assert "secret" not in warnings[0]


@pytest.mark.parametrize(
    ("stdout", "stderr"),
    [(b"stdout-secret", b"ECONNREFUSED stderr-secret"), (b"ECONNREFUSED stdout-secret", b"stderr-secret")],
)
def test_docker_execute_redacts_captured_connection_failure(monkeypatch, stdout: bytes, stderr: bytes) -> None:
    agent_id = uuid.uuid4()
    agent: Any = SimpleNamespace(id=agent_id, container_id="container-abc")

    class FakeDocker(OwnedGuestInspect):
        def __init__(self) -> None:
            super().__init__(agent_id)
            self.container = self

        def execute(self, container, argv, stream=False):
            raise DockerException(["docker", "exec", "command-secret"], 1, stdout=stdout, stderr=stderr)

    monkeypatch.setattr("app.services.agent_manager.agent_manager.docker_client", FakeDocker())

    error = openclaw_inbox._docker_execute(agent, ["node"])
    assert error is not None
    assert openclaw_inbox._hooks_unreachable(error)
    assert "secret" not in error


@pytest.mark.parametrize(
    ("code", "stderr", "refused"),
    [
        (BOOTSTRAP_PENDING_EXIT, PENDING_STDERR, True),
        (BOOTSTRAP_PENDING_EXIT, None, False),
        (BOOTSTRAP_PENDING_EXIT, "openclaw: temporary failure\n", False),
        (BOOTSTRAP_PENDING_EXIT, f"warning\n{PENDING_STDERR}", False),
        (1, PENDING_STDERR, False),
    ],
    ids=["helper-refusal", "action-75-silent", "action-75-other-stderr", "marker-not-first", "marker-other-code"],
)
def test_docker_execute_reports_only_the_helper_refusal_as_bootstrap_pending(
    monkeypatch, code: int, stderr: str | None, refused: bool
) -> None:
    # Given `exec --` preserves the action's exit status, so exit 75 alone can come from an admitted action.
    agent_id = uuid.uuid4()
    agent: Any = SimpleNamespace(id=agent_id, container_id="container-abc")

    class FakeDocker(OwnedGuestInspect):
        def __init__(self) -> None:
            super().__init__(agent_id)
            self.container = self

        def execute(self, container, argv, stream=False):
            raise DockerException(["docker", "exec"], code, stderr=None if stderr is None else stderr.encode())

    monkeypatch.setattr("app.services.agent_manager.agent_manager.docker_client", FakeDocker())
    # When the exec fails.
    error = openclaw_inbox._docker_execute(agent, ["node"])
    # Then only the helper's own refusal is reported as pending bootstrap.
    assert (error == openclaw_inbox.BOOTSTRAP_PENDING) is refused


def test_wake_body_includes_message_id_for_direct_report() -> None:
    message_id = uuid.uuid4()
    body = openclaw_inbox._wake_body("Please introduce yourself.", message_id=message_id)
    assert str(message_id) in body["message"]
    assert "/api/gateway/report" in body["message"]
    assert "Please introduce yourself." in body["message"]
