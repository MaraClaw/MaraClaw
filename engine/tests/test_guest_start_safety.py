"""Start boundaries exercised with Docker's create-before-bind failure behavior."""

from __future__ import annotations

import builtins
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from python_on_whales.exceptions import DockerException, NoSuchContainer

from app.records.agent import AgentRecord
from app.services import agent_manager as module
from app.services.agent_manager import AgentManager
from app.services.guest_admission import GuestStartPreflightError
from app.services.guest_identity import AGENT_LABEL, GuestOwnershipError, guest_container_name


class Docker:
    def __init__(self) -> None:
        self.container = self
        self.guests: dict[str, SimpleNamespace] = {}
        self.runs: list[dict[str, object]] = []
        self.removed: list[str] = []
        self.failures: list[bytes] = []
        self.listings: list[object] = []

    def add(self, agent: AgentRecord, *, memory: int = 20, status: str = "running") -> SimpleNamespace:
        guest = SimpleNamespace(
            id=agent.container_id or f"created-{len(self.runs)}",
            name=guest_container_name(agent.id),
            config=SimpleNamespace(labels={AGENT_LABEL: str(agent.id)}),
            host_config=SimpleNamespace(memory=memory, port_bindings={}),
            state=SimpleNamespace(status=status),
        )
        self.guests[guest.id] = guest
        return guest

    def inspect(self, reference: str) -> SimpleNamespace:
        for guest in self.guests.values():
            if reference in (guest.id, guest.name):
                return guest
        raise NoSuchContainer(["docker", "inspect", reference], 1)

    def stop(self, reference: str, **_kwargs: object) -> None:
        self.inspect(reference).state.status = "exited"

    def remove(self, references: builtins.list[str]) -> None:
        for reference in references:
            self.removed.append(reference)
            del self.guests[reference]

    def list(self, *, all: bool, filters: object) -> builtins.list[SimpleNamespace]:
        assert all
        self.listings.append(filters)
        return [guest for guest in self.guests.values() if guest.state.status in ("running", "restarting")]

    def run(self, _image: str, _command: builtins.list[str], **kwargs: object) -> SimpleNamespace:
        self.runs.append(kwargs)
        labels = kwargs["labels"]
        assert isinstance(labels, dict)
        agent = AgentRecord(id=module.uuid.UUID(labels[AGENT_LABEL]), name="new", creator_id=uuid4())
        guest = self.add(agent)
        if self.failures:
            raise DockerException(["docker", "run"], 125, stderr=self.failures.pop(0))
        return guest


@pytest.fixture
def env(tmp_path, monkeypatch):
    docker = Docker()
    manager = AgentManager.__new__(AgentManager)
    monkeypatch.setattr(manager, "docker_client", docker, raising=False)
    agent = AgentRecord(
        id=uuid4(), name="guest", creator_id=uuid4(), status="running", container_id="old", container_port=18888
    )
    directory = tmp_path / str(agent.id)
    directory.mkdir()
    monkeypatch.setattr(manager, "_agent_dir", lambda _id: directory)

    async def materialize(_id):
        return directory

    monkeypatch.setattr(manager, "_materialize_agent_dir", materialize)
    monkeypatch.setattr(module.settings, "GOGCLI_ENABLED", False)
    monkeypatch.setitem(module.settings.__dict__, "OPENCLAW_GUEST_AGGREGATE_MEMORY_LIMIT_BYTES", 0)
    monkeypatch.setattr(module.settings, "OPENCLAW_GUEST_MEMORY_LIMIT_BYTES", 20)
    docker.add(agent)
    return manager, docker, agent


async def test_preflight_failure_preserves_old_guest(env, monkeypatch):
    # Given an existing guest and a failing final preparation step.
    manager, docker, agent = env

    def fail(_directory):
        raise OSError("disk full")

    monkeypatch.setattr(module, "write_maraclaw_sync_skill", fail)
    # When preparation fails (the HTTP boundary supplies the typed response).
    with pytest.raises(GuestStartPreflightError):
        await manager.start_container(agent)
    # Then no runtime reference or old guest was destroyed.
    assert (agent.status, agent.container_id, agent.container_port) == ("running", "old", 18888)
    assert docker.removed == []


async def test_new_guest_gets_limits_and_preferred_port(env):
    # Given an existing valid host port.
    manager, docker, agent = env
    # When replacing the guest.
    result = await manager.start_container(agent)
    # Then Docker gets hard limits, no extra swap, and the previous host port.
    assert result == agent.container_id
    run = docker.runs[0]
    assert (run.get("memory"), run.get("memory_swap"), run.get("cpus"), run.get("pids_limit")) == (20, 20, 1.0, 512)
    assert run["publish"] == [(18888, module.settings.OPENCLAW_GATEWAY_PORT)]
    assert docker.listings == []


@pytest.mark.parametrize(
    "failure",
    [
        b"port is already allocated",
        b"bind: address already in use",
        b"Bind for 0.0.0.0:18888 failed: port is already allocated",
    ],
)
async def test_bind_conflict_cleans_created_guest_before_retry(env, failure):
    # Given Docker creates a container before failing its bind.
    manager, docker, agent = env
    docker.failures = [failure]
    # When starting.
    result = await manager.start_container(agent)
    # Then the failed creation is removed and another bounded port succeeds.
    assert result is not None
    assert len(docker.runs) == 2
    assert docker.removed == ["old", "created-1"]
    assert docker.runs[0]["publish"] != docker.runs[1]["publish"]


@pytest.mark.parametrize(("failure", "attempts"), [(b"port is already allocated", 8), (b"invalid image format", 1)])
async def test_failed_run_cleans_leftovers_and_clears_both_runtime_records(env, monkeypatch, failure, attempts):
    # Given model preparation refreshes the record and Docker keeps failing.
    manager, docker, original = env
    refreshed = replace(original)
    docker.failures = [failure] * 8

    async def refresh(_agent):
        return refreshed

    monkeypatch.setattr("app.services.enterprise_llm.ensure_agent_company_models", refresh)
    # When starting.
    result = await manager.start_container(original)
    # Then attempts are bounded, all created guests removed, and neither record claims an old runtime.
    assert result is None
    assert len(docker.runs) == attempts
    assert docker.guests == {}
    for target in (original, refreshed):
        assert (target.status, target.container_id, target.container_port) == ("error", None, None)


@pytest.mark.parametrize("operation", ["stop_container", "remove_container"])
async def test_ownership_failure_returns_false_without_mutating_runtime(env, operation):
    # Given a recorded container now belongs to another agent.
    manager, docker, agent = env
    docker.guests["old"].config.labels[AGENT_LABEL] = str(uuid4())
    # When a background caller requests stop/removal.
    result = await getattr(manager, operation)(agent)
    # Then the refusal is safe and references survive.
    assert result is False
    assert (agent.status, agent.container_id, agent.container_port) == ("running", "old", 18888)
    assert docker.removed == []


async def test_start_ownership_failure_preserves_references(env):
    # Given a foreign recorded guest.
    manager, docker, agent = env
    docker.guests["old"].config.labels[AGENT_LABEL] = str(uuid4())
    # When replacement is requested.
    with pytest.raises(GuestOwnershipError):
        await manager.start_container(agent)
    # Then identity guard preflight permits no mutation.
    assert (agent.status, agent.container_id, agent.container_port) == ("running", "old", 18888)
    assert docker.runs == []


async def test_ownership_inspection_io_failure_preserves_old_runtime(env, monkeypatch):
    # Given Docker fails before ownership preflight can establish replacement targets.
    manager, docker, agent = env

    def unavailable(_reference):
        raise DockerException(["docker", "inspect"], 1, stderr=b"daemon unavailable")

    monkeypatch.setattr(docker, "inspect", unavailable)
    # When replacement cannot safely begin.
    with pytest.raises(GuestStartPreflightError):
        await manager.start_container(agent)
    # Then the old runtime is not falsely cleared or persisted as a failed replacement.
    assert (agent.status, agent.container_id, agent.container_port) == ("running", "old", 18888)
    assert docker.removed == docker.runs == []


async def test_cleanup_ownership_refusal_after_replacement_clears_stale_runtime(env, monkeypatch):
    # Given a failed run whose leftover is no longer label-proven to belong to this agent.
    manager, docker, agent = env
    docker.failures = [b"port is already allocated"]
    run = docker.run

    def failed_run(*args, **kwargs):
        try:
            return run(*args, **kwargs)
        finally:
            docker.guests["created-1"].config.labels[AGENT_LABEL] = str(uuid4())

    monkeypatch.setattr(docker, "run", failed_run)
    # When cleanup refuses the foreign leftover after the old guest has been removed.
    result = await manager.start_container(agent)
    # Then no retry or foreign deletion occurs, and the removed old runtime is not retained.
    assert result is None
    assert (agent.status, agent.container_id, agent.container_port) == ("error", None, None)
    assert docker.removed == ["old"]
    assert len(docker.runs) == 1
