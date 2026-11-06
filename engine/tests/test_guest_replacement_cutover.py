"""Replacement writes guest state only after the old owned guests are stopped."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Literal, assert_never
from uuid import uuid4

import pytest
from pywhales.exceptions import DockerException
from tests.test_guest_start_safety import env

from app.core.domain.agents import AgentStatus
from app.services import agent_manager as module, guest_admission as admission
from app.services.guest_admission import GuestAdmissionError, GuestStartPreflightError
from app.services.guest_identity import AGENT_LABEL, GuestOwnershipError

__all__ = ["env"]

type Refusal = Literal["foreign_owner", "ownership_inspection", "capacity"]

LIVE_FILES = {
    "openclaw.json": b'{"plugins": {"entries": {"custom": {"enabled": true}}}}',
    ".openclaw-gateway-token": b"live-gateway-token",
    "memory/main.sqlite": b"SQLite format 3\x00native-state\xff",
}


def _tree(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def _seed(root: Path) -> dict[str, bytes]:
    for name, content in LIVE_FILES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return _tree(root)


@pytest.mark.parametrize("refusal", ["foreign_owner", "ownership_inspection", "capacity"])
async def test_refusal_before_cutover_leaves_the_live_tree_byte_identical(env, monkeypatch, refusal: Refusal):
    # Given live guest state and a replacement that is refused before any guest is stopped.
    manager, docker, agent = env
    directory = manager._agent_dir(agent.id)
    before = _seed(directory)

    def unavailable(_reference):
        raise DockerException(["docker", "inspect"], 1, stderr=b"daemon unavailable")

    match refusal:
        case "foreign_owner":
            docker.guests["old"].config.labels[AGENT_LABEL] = str(uuid4())
            refused = GuestOwnershipError
        case "ownership_inspection":
            monkeypatch.setattr(docker, "inspect", unavailable)
            refused = GuestStartPreflightError
        case "capacity":
            monkeypatch.setattr(admission.settings, "OPENCLAW_GUEST_AGGREGATE_MEMORY_LIMIT_BYTES", 40)
            docker.add(replace(agent, id=uuid4(), container_id="other"), memory=21)
            refused = GuestAdmissionError
        case unreachable:
            assert_never(unreachable)
    # When the replacement is refused.
    with pytest.raises(refused):
        await manager.replace_container(agent)
    # Then the old guest and every one of its files are exactly as they were.
    assert _tree(directory) == before
    assert docker.removed == docker.runs == []
    assert (agent.status, agent.container_id, agent.container_port) == ("running", "old", 18888)


async def test_failed_stop_applies_no_state_and_claims_no_runtime(env, monkeypatch):
    # Given live guest state and a Docker daemon that cannot stop the old guest.
    manager, docker, agent = env
    directory = manager._agent_dir(agent.id)
    before = _seed(directory)

    def refuse(_reference, **_kwargs):
        raise DockerException(["docker", "stop"], 1, stderr=b"daemon unavailable")

    monkeypatch.setattr(docker, "stop", refuse)
    # When the replacement tries to stop it.
    result = await manager.replace_container(agent)
    # Then no state was applied beside a guest that may still be writing, and nothing new started.
    assert result is None
    assert _tree(directory) == before
    assert docker.runs == []
    assert (agent.status, agent.container_id, agent.container_port) == (AgentStatus.ERROR, None, None)


async def test_state_is_applied_only_between_the_old_guest_stopping_and_the_new_one_running(env, monkeypatch):
    # Given live guest state, a storage hydration and a gogcli restore that both write into the mounted tree.
    manager, docker, agent = env
    directory = manager._agent_dir(agent.id)
    before = _seed(directory)
    events: list[str] = []
    trees: dict[str, dict[str, bytes]] = {}
    agent.gogcli_enabled = True
    monkeypatch.setattr(module.settings, "GOGCLI_ENABLED", True)

    async def hydrate(_agent_id):
        events.append("hydrate")
        (directory / "memory" / "hydrated.md").write_bytes(b"hydrated")
        return directory

    async def restore(_agent_id, _directory):
        events.append("gogcli")
        return True

    stop, run = docker.stop, docker.run

    def observed_stop(reference, **kwargs):
        events.append("stop")
        trees["stop"] = _tree(directory)
        stop(reference, **kwargs)

    def observed_run(image, command, **kwargs):
        events.append("run")
        trees["run"] = _tree(directory)
        return run(image, command, **kwargs)

    monkeypatch.setattr(manager, "_materialize_agent_dir", hydrate)
    monkeypatch.setattr(module, "restore_gogcli_state", restore)
    monkeypatch.setattr(docker, "stop", observed_stop)
    monkeypatch.setattr(docker, "run", observed_run)
    # When the guest is replaced.
    result = await manager.replace_container(agent)
    # Then the tree is untouched while the old guest could still write, and complete once the new one starts.
    assert result == "created-1"
    assert events == ["stop", "hydrate", "gogcli", "run"]
    assert trees["stop"] == before
    assert trees["run"]["memory/hydrated.md"] == b"hydrated"
    assert trees["run"]["openclaw.json"] != before["openclaw.json"]
    assert trees["run"][".openclaw-gateway-token"] == before[".openclaw-gateway-token"]
    assert ".maraclaw-rpc.mjs" in trees["run"]


async def test_state_failure_after_the_old_guest_stopped_reports_no_runtime(env, monkeypatch):
    # Given live guest state and storage that fails once the old guest is already gone.
    manager, docker, agent = env
    directory = manager._agent_dir(agent.id)
    before = _seed(directory)

    async def unavailable(_agent_id):
        raise OSError("storage unavailable")

    monkeypatch.setattr(manager, "_materialize_agent_dir", unavailable)
    # When the replacement applies its state.
    with pytest.raises(GuestStartPreflightError) as failure:
        await manager.replace_container(agent)
    # Then the removed guest is not claimed, no replacement started, and a retry can recreate it.
    assert failure.value.code == "runtime_failed"
    assert docker.removed == ["old"]
    assert docker.runs == []
    assert (agent.status, agent.container_id, agent.container_port) == (AgentStatus.ERROR, None, None)
    assert _tree(directory) == before


async def test_state_failure_without_an_old_guest_is_a_plain_preparation_failure(env, monkeypatch):
    # Given no guest exists yet and storage fails while the first one's state is applied.
    manager, docker, agent = env
    docker.guests.clear()
    agent.container_id = None
    agent.container_port = None
    agent.status = AgentStatus.STOPPED

    async def unavailable(_agent_id):
        raise OSError("storage unavailable")

    monkeypatch.setattr(manager, "_materialize_agent_dir", unavailable)
    # When creation applies its state.
    with pytest.raises(GuestStartPreflightError) as failure:
        await manager.replace_container(agent, create_only=True)
    # Then it stays the retry-free configuration failure, and no runtime is claimed or destroyed.
    assert failure.value.code == "configuration_failed"
    assert docker.removed == docker.runs == []
    assert (agent.status, agent.container_id, agent.container_port) == (AgentStatus.STOPPED, None, None)
