"""Start boundaries exercised with Docker's create-before-bind failure behavior."""

from __future__ import annotations

import builtins
from dataclasses import replace
from types import SimpleNamespace
from typing import Unpack
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pywhales.client_config import ParsingError
from pywhales.exceptions import DockerException, NoSuchContainer

from app.core.domain.agents import AgentStatus
from app.dao.agent_mcp_dao import agent_mcp_server_dao
from app.records.agent import AgentRecord
from app.services import agent_manager as module, guest_admission as admission
from app.services.agent_manager import AgentManager
from app.services.guest_admission import GuestAdmissionError, GuestStartPreflightError
from app.services.guest_identity import AGENT_LABEL, GuestOwnershipError, guest_container_name
from builtin_keyword_fakes import IncludeAllKeyword


class Docker:
    def __init__(self) -> None:
        self.container = self
        self.image = self
        self.guests: dict[str, SimpleNamespace] = {}
        self.runs: list[dict[str, object]] = []
        self.removed: list[str] = []
        self.failures: list[bytes] = []
        self.listings: list[object] = []
        self.image_present = True
        self.exists_failure: Exception | None = None
        self.pull_failure: DockerException | None = None
        self.pulls: list[tuple[str, dict[str, object]]] = []
        self.events: list[str] = []

    def exists(self, _reference: str) -> bool:
        if self.exists_failure is not None:
            raise self.exists_failure
        return self.image_present

    def pull(self, reference: str, **kwargs: object) -> None:
        self.events.append("pull")
        self.pulls.append((reference, kwargs))
        if self.pull_failure is not None:
            raise self.pull_failure
        self.image_present = True

    def add(self, agent: AgentRecord, *, memory: int = 20, status: str = "running") -> SimpleNamespace:
        guest = SimpleNamespace(
            id=agent.container_id or f"created-{len(self.runs)}",
            name=guest_container_name(agent.id),
            config=SimpleNamespace(labels={AGENT_LABEL: str(agent.id)}),
            host_config=SimpleNamespace(memory=memory, port_bindings={}),
            state=SimpleNamespace(status=status, restarting=False),
            restart_count=0,
            mounts=[SimpleNamespace(destination="/home/node/custom-state")],
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

    def list(self, *, filters: object, **kwargs: Unpack[IncludeAllKeyword]) -> builtins.list[SimpleNamespace]:
        assert kwargs["all"]
        self.events.append("list")
        self.listings.append(filters)
        return [guest for guest in self.guests.values() if guest.state.status in ("running", "restarting")]

    def run(self, _image: str, _command: builtins.list[str], **kwargs: object) -> SimpleNamespace:
        self.events.append("run")
        self.runs.append(kwargs)
        labels = kwargs["labels"]
        assert isinstance(labels, dict)
        agent = AgentRecord(id=module.uuid.UUID(labels[AGENT_LABEL]), name="new", creator_id=uuid4())
        guest = self.add(agent)
        guest.config.labels = dict(labels)
        if self.failures:
            raise DockerException(["docker", "run"], 125, stderr=self.failures.pop(0))
        return guest


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_mcp_server_dao, "list_for_agent", AsyncMock(return_value=[]))
    docker = Docker()
    manager = AgentManager.__new__(AgentManager)
    monkeypatch.setattr(manager, "docker_client", docker, raising=False)
    monkeypatch.setattr(manager, "_prepare_tavily_proxy_token", AsyncMock())
    agent = AgentRecord(
        id=uuid4(),
        name="guest",
        creator_id=uuid4(),
        status=AgentStatus.RUNNING,
        container_id="old",
        container_port=18888,
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
    # Given an existing guest with on-disk state and a failing final preparation step.
    manager, docker, agent = env
    directory = manager._agent_dir(agent.id)
    config = directory / "openclaw.json"
    old_config = '{"plugins": {"entries": {"custom": {"enabled": true}}}}'
    config.write_text(old_config, encoding="utf-8")
    old_listing = sorted(path.name for path in directory.iterdir())

    def fail(_directory):
        raise OSError("disk full")

    monkeypatch.setattr(module, "prepare_native_gateway_files", fail)
    # When preparation fails (the HTTP boundary supplies the typed response).
    with pytest.raises(GuestStartPreflightError):
        await manager.replace_container(agent)
    # Then no runtime reference or old guest was destroyed, and the guest's files are exactly as it left them.
    assert (agent.status, agent.container_id, agent.container_port) == ("running", "old", 18888)
    assert docker.removed == []
    assert config.read_text(encoding="utf-8") == old_config
    assert sorted(path.name for path in directory.iterdir()) == old_listing


async def test_token_persistence_failure_precedes_guest_mutation(env, monkeypatch):
    manager, docker, agent = env

    async def fail(_agent_id):
        raise RuntimeError("persistence failed")

    monkeypatch.setattr(manager, "_prepare_tavily_proxy_token", fail, raising=False)
    materialize = AsyncMock()
    monkeypatch.setattr(manager, "_materialize_agent_dir", materialize)
    with pytest.raises(RuntimeError, match="persistence failed"):
        await manager.replace_container(agent)
    materialize.assert_not_awaited()
    assert docker.removed == docker.runs == []


async def test_no_docker_does_not_issue_token(env, monkeypatch):
    manager, _docker, agent = env
    monkeypatch.setattr(manager, "docker_client", None)
    assert await manager.replace_container(agent) is None
    manager._prepare_tavily_proxy_token.assert_not_awaited()


async def test_disabled_proxy_does_not_issue_token(env, monkeypatch):
    manager, _docker, agent = env
    monkeypatch.setattr(module.settings, "TAVILY_PROXY_ENABLED", False)
    monkeypatch.delattr(manager, "_prepare_tavily_proxy_token")
    issue = AsyncMock()
    monkeypatch.setattr("app.services.tavily.tokens.make_proxy_token", issue)
    await manager._prepare_tavily_proxy_token(agent.id)
    issue.assert_not_awaited()


@pytest.mark.parametrize("missing", ["state", "host_config"])
async def test_resume_refuses_incomplete_inspection(env, missing):
    manager, docker, agent = env
    docker.guests["old"].state.status = "exited"
    setattr(docker.guests["old"], missing, None)
    with pytest.raises(GuestStartPreflightError if missing == "state" else GuestAdmissionError):
        await manager.start_container(agent)
    assert docker.removed == docker.runs == []
    assert agent.container_id == "old"


async def test_new_guest_gets_limits_and_preferred_port(env):
    # Given an existing valid host port.
    manager, docker, agent = env
    # When replacing the guest.
    result = await manager.replace_container(agent)
    # Then Docker gets hard limits, no extra swap, and the previous host port.
    assert result == agent.container_id
    run = docker.runs[0]
    assert (run.get("memory"), run.get("memory_swap"), run.get("cpus"), run.get("pids_limit")) == (20, 20, 1.0, 512)
    assert run["publish"] == [("127.0.0.1:18888", module.settings.OPENCLAW_GATEWAY_PORT)]
    assert docker.listings == []


@pytest.mark.parametrize(
    ("reference", "present", "failure", "pulls", "replaced"),
    [
        pytest.param("registry.example/team/openclaw:1", True, None, 0, True, id="present"),
        pytest.param("registry.example/team/openclaw:1", False, None, 1, True, id="registry-missing-is-pulled"),
        pytest.param("registry.example/team/openclaw:1", False, "pull", 1, False, id="registry-pull-fails"),
        pytest.param("registry.example/team/openclaw:1", False, "inspect", 0, False, id="registry-inspect-malformed"),
        pytest.param("arm64-openclaw:2026.11.3", False, None, 0, False, id="local-missing-is-never-pulled"),
        pytest.param(f"sha256:{'a' * 64}", False, None, 0, False, id="image-id-missing-is-never-pulled"),
    ],
)
async def test_guest_image_is_settled_before_admission(env, monkeypatch, reference, present, failure, pulls, replaced):
    # Given aggregate admission is on, a running guest, and a replacement image in the given local state.
    manager, docker, agent = env
    monkeypatch.setattr(admission.settings, "OPENCLAW_GUEST_AGGREGATE_MEMORY_LIMIT_BYTES", 40)
    docker.image_present = present
    if failure == "pull":
        docker.pull_failure = DockerException(["docker", "pull", reference], 1, stderr=b"pull access denied")
    if failure == "inspect":
        docker.exists_failure = ParsingError("Missing inspect identity")
    # When the guest is replaced onto that image.
    if replaced:
        result = await manager.replace_container(agent, image=reference)
        # Then a missing registry image is pulled once under the configured deadline, strictly before admission
        # lists the host's guests and before `run`, which never pulls implicitly.
        assert result == agent.container_id
        assert docker.removed == ["old"]
        assert docker.events == [*["pull"] * pulls, "list", "run"]
        assert docker.runs[0].get("pull") == "never"
    else:
        with pytest.raises(GuestStartPreflightError) as refused:
            await manager.replace_container(agent, image=reference)
        # Then the failure precedes admission and every destructive step: nothing is listed, stopped or created.
        assert refused.value.code == "runtime_failed"
        assert (agent.status, agent.container_id, agent.container_port) == ("running", "old", 18888)
        assert docker.removed == docker.runs == docker.listings == []
        assert docker.events == ["pull"] * pulls
    assert [(pulled, kwargs.get("timeout")) for pulled, kwargs in docker.pulls] == [
        (reference, module.settings.OPENCLAW_GUEST_IMAGE_PULL_TIMEOUT_SECONDS)
    ] * pulls


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
    result = await manager.replace_container(agent)
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
    result = await manager.replace_container(original)
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
        await manager.replace_container(agent)
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
        await manager.replace_container(agent)
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
    result = await manager.replace_container(agent)
    # Then no retry or foreign deletion occurs, and the removed old runtime is not retained.
    assert result is None
    assert (agent.status, agent.container_id, agent.container_port) == ("error", None, None)
    assert docker.removed == ["old"]
    assert len(docker.runs) == 1
