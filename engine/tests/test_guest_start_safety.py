"""Start boundaries exercised with Docker's create-before-bind failure behavior."""

from __future__ import annotations

import builtins
import threading
from dataclasses import replace
from types import SimpleNamespace
from typing import Unpack
from unittest.mock import AsyncMock
from uuid import uuid4

import anyio
import anyio.lowlevel
import pytest
from pywhales import ClientNotFoundError
from pywhales.client_config import ParsingError
from pywhales.exceptions import DockerException, NoSuchContainer

from app.core.domain.agents import AgentStatus
from app.dao.agent_mcp_dao import agent_mcp_server_dao
from app.dao.agent_workspace_usage_dao import agent_workspace_usage_dao
from app.records.agent import AgentRecord
from app.services import agent_manager as module, agent_materialize, guest_admission as admission, guest_readiness
from app.services.agent_manager import AgentManager
from app.services.agent_start_store import start_store
from app.services.guest_admission import GuestAdmissionError, GuestStartPreflightError, GuestStartStoppedError
from app.services.guest_identity import AGENT_LABEL, GuestOwnershipError, guest_container_name
from app.services.workspace_mirror import MirrorMode, MirrorReconcileError, ReconcileReport
from builtin_keyword_fakes import IncludeAllKeyword


class Docker:
    def __init__(self) -> None:
        self.container = self
        self.image = self
        self.guests: dict[str, SimpleNamespace] = {}
        self.runs: list[dict[str, object]] = []
        self.images: list[str] = []
        self.removed: list[str] = []
        self.failures: list[bytes] = []
        self.listings: list[object] = []
        self.image_present = True
        self.exists_failure: Exception | None = None
        self.pull_failure: DockerException | None = None
        self.pulls: list[tuple[str, dict[str, object]]] = []
        self.events: list[str] = []
        self.updates: list[tuple[str, dict[str, object]]] = []
        self.update_failure: DockerException | None = None
        self.system = self
        self.logging_driver: str | None = "json-file"
        self.info_failure: Exception | None = None
        self.infos = 0

    def info(self) -> SimpleNamespace:
        self.infos += 1
        if self.info_failure is not None:
            raise self.info_failure
        return SimpleNamespace(logging_driver=self.logging_driver)

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
            host_config=SimpleNamespace(
                memory=memory,
                memory_swap=memory,
                nano_cpus=1_000_000_000,
                pids_limit=512,
                cpu_shares=0,
                port_bindings={},
            ),
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

    def update(self, reference: str, **kwargs: object) -> None:
        self.events.append("update")
        self.updates.append((reference, kwargs))
        if self.update_failure is not None:
            raise self.update_failure
        host_config = self.inspect(reference).host_config
        for name, value in kwargs.items():
            if name == "cpus":
                assert isinstance(value, float)
                host_config.nano_cpus = round(value * 1_000_000_000)
            else:
                setattr(host_config, name, value)

    def remove(self, references: builtins.list[str]) -> None:
        for reference in references:
            self.removed.append(reference)
            del self.guests[reference]

    def list(self, *, filters: object, **kwargs: Unpack[IncludeAllKeyword]) -> builtins.list[SimpleNamespace]:
        assert kwargs["all"]
        self.events.append("list")
        self.listings.append(filters)
        return [guest for guest in self.guests.values() if guest.state.status in ("running", "restarting")]

    def run(self, image: str, _command: builtins.list[str], **kwargs: object) -> SimpleNamespace:
        self.events.append("run")
        self.images.append(image)
        self.runs.append(kwargs)
        labels = kwargs["labels"]
        assert isinstance(labels, dict)
        agent = AgentRecord(id=module.uuid.UUID(labels[AGENT_LABEL]), name="new", creator_id=uuid4())
        guest = self.add(agent)
        guest.config.labels = dict(labels)
        if self.failures:
            raise DockerException(["docker", "run"], 125, stderr=self.failures.pop(0))
        return guest


class BlockedProbe:
    """A readiness probe whose lane thread blocks on a threading.Event and wakes the loop once it is blocked."""

    def __init__(self) -> None:
        self.entered = anyio.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self._token = anyio.lowlevel.current_token()

    def __call__(self, _client: object, _reference: str, _timeout: float, _child: object) -> bool:
        anyio.from_thread.run_sync(self.entered.set, token=self._token)
        try:
            return self.release.wait(10)
        finally:
            self.finished.set()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_mcp_server_dao, "list_for_agent", AsyncMock(return_value=[]))
    monkeypatch.setattr(agent_workspace_usage_dao, "get", AsyncMock(return_value=None))
    docker = Docker()
    manager = AgentManager.__new__(AgentManager)
    monkeypatch.setattr(manager, "docker_client", docker, raising=False)
    monkeypatch.setattr(manager, "_prepare_tavily_proxy_token", AsyncMock())
    # Start checkpoints read the durable operation; offline starts are never stopped unless a test says so.
    monkeypatch.setattr(start_store, "halted", AsyncMock(return_value=None))
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


@pytest.mark.parametrize("stop_at", ["pull", "create"])
async def test_stopped_start_creates_no_container(env, monkeypatch, stop_at):
    # Given a creating start whose image must be pulled, stopped before the pull or while its state is prepared.
    manager, docker, agent = env
    docker.guests.clear()
    agent.container_id = None
    docker.image_present = False
    operation = uuid4()
    stopped = stop_at == "pull"
    prepare = manager._apply_guest_state

    async def preparing(*args, **kwargs):
        nonlocal stopped
        result = await prepare(*args, **kwargs)
        stopped = True
        return result

    async def halted(operation_id):
        assert operation_id == operation
        return "cancelled" if stopped else None

    monkeypatch.setattr(manager, "_apply_guest_state", preparing)
    monkeypatch.setattr(start_store, "halted", halted)
    # When the start reaches its next checkpoint.
    with pytest.raises(GuestStartStoppedError) as stop:
        await manager.replace_container(
            agent, image="registry.example/team/openclaw:1", create_only=True, operation_id=operation
        )
    # Then it ends there: a stop seen before the pull skips it, and no container is created after a stop is seen.
    assert stop.value.reason == "cancelled"
    assert len(docker.pulls) == (0 if stop_at == "pull" else 1)
    assert docker.runs == []


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
    assert "cpu_shares" not in run
    assert run["publish"] == [("127.0.0.1:18888", module.settings.OPENCLAW_GATEWAY_PORT)]
    assert docker.listings == []


@pytest.mark.parametrize("driver", ["json-file", "local"])
async def test_new_guest_logs_are_bounded_under_a_size_rotating_driver(env, driver):
    # Given a daemon whose default logging driver rotates by size.
    manager, docker, agent = env
    docker.logging_driver = driver
    # When the guest is created.
    await manager.replace_container(agent)
    # Then Docker gets the default size and file bound and no explicit driver, so the daemon default still applies.
    run = docker.runs[0]
    assert run["log_options"] == ["max-size=10m", "max-file=3"]
    assert "log_driver" not in run


async def test_new_guest_log_bound_follows_the_settings(env, monkeypatch):
    # Given an operator who changed the guest log bound.
    manager, docker, agent = env
    monkeypatch.setattr(module.settings, "OPENCLAW_GUEST_LOG_MAX_SIZE", "50m")
    monkeypatch.setattr(module.settings, "OPENCLAW_GUEST_LOG_MAX_FILE", 5)
    # When the guest is created.
    await manager.replace_container(agent)
    # Then Docker gets the configured bound.
    assert docker.runs[0]["log_options"] == ["max-size=50m", "max-file=5"]


@pytest.mark.parametrize("driver", ["journald", "awslogs", "syslog", "none", None])
async def test_new_guest_gets_no_log_options_under_any_other_driver(env, driver):
    # Given a daemon whose default driver rejects max-size, or reports no driver at all.
    manager, docker, agent = env
    docker.logging_driver = driver
    # When the guest is created.
    result = await manager.replace_container(agent)
    # Then it still starts, and Docker gets neither log options nor a driver.
    assert result == agent.container_id
    assert "log_options" not in docker.runs[0]
    assert "log_driver" not in docker.runs[0]


@pytest.mark.parametrize(
    "failure",
    [
        DockerException(["docker", "system", "info"], 1, stderr=b"daemon unreachable"),
        ParsingError("Malformed system info"),
        ClientNotFoundError("docker"),
    ],
    ids=["docker-error", "malformed-info", "no-cli"],
)
async def test_new_guest_starts_without_log_options_when_the_daemon_driver_is_unknown(env, failure):
    # Given a daemon whose driver cannot be read.
    manager, docker, agent = env
    docker.info_failure = failure
    # When the guest is created.
    result = await manager.replace_container(agent)
    # Then the start is not failed over logging, and no size option is guessed.
    assert result == agent.container_id
    assert "log_options" not in docker.runs[0]


@pytest.mark.parametrize(
    ("reference", "present", "failure", "pulls", "replaced"),
    [
        pytest.param("registry.example/team/openclaw:1", True, None, 0, True, id="present"),
        pytest.param("registry.example/team/openclaw:1", False, None, 1, True, id="registry-missing-is-pulled"),
        pytest.param("registry.example/team/openclaw:1", False, "pull", 1, False, id="registry-pull-fails"),
        pytest.param("registry.example/team/openclaw:1", False, "inspect", 0, False, id="registry-inspect-malformed"),
        pytest.param("arm64-openclaw:2026.11.10", False, None, 0, False, id="local-missing-is-never-pulled"),
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


async def test_guest_docker_calls_proceed_while_default_worker_threads_are_exhausted(env, monkeypatch):
    # Given anyio's default limiter has one token, held by a worker thread blocked on a threading.Event.
    manager, docker, agent = env
    limiter = anyio.to_thread.current_default_thread_limiter()
    entered = anyio.Event()
    release = threading.Event()

    def hold() -> bool:
        anyio.from_thread.run_sync(entered.set)
        return release.wait(10)

    monkeypatch.setattr(guest_readiness, "probe_guest", lambda *_args: True)
    monkeypatch.setattr(guest_readiness, "READY_INTERVAL_SECONDS", 0)
    tokens = limiter.total_tokens
    limiter.total_tokens = 1
    try:
        with anyio.fail_after(5):
            async with anyio.create_task_group() as group:
                group.start_soon(anyio.to_thread.run_sync, hold)
                await entered.wait()
                # When readiness, Stop and removal make their Docker calls.
                ready = await guest_readiness.guest_ready(docker, "old")
                stopped = await manager.stop_container(agent)
                removed = await manager.remove_container(agent)
                release.set()
                # Then each completed on the Docker lane without waiting for a default worker thread.
                assert (ready, stopped, removed) == (True, True, True)
                assert docker.removed == ["old"]
    finally:
        release.set()
        limiter.total_tokens = tokens


async def test_cancelled_start_returns_while_its_readiness_probe_is_still_blocked(env, monkeypatch):
    # Given Start reuses the running guest, and its readiness probe blocks on a threading.Event in a lane thread.
    manager, docker, agent = env
    probe = BlockedProbe()
    returned: list[bool] = []

    async def start():
        try:
            _ = await manager.start_container(agent)
        finally:
            returned.append(probe.finished.is_set())

    monkeypatch.setattr(guest_readiness, "probe_guest", probe)
    try:
        with anyio.fail_after(5):
            async with anyio.create_task_group() as group:
                group.start_soon(start)
                await probe.entered.wait()
                # When the start is cancelled while the probe is still blocked.
                group.cancel_scope.cancel()
    finally:
        probe.release.set()
    # Then Start gave control back before the probe's thread returned, and the guest was left running.
    assert returned == [False]
    assert docker.guests["old"].state.status == "running"


def mirror_active(
    monkeypatch: pytest.MonkeyPatch,
    manager: AgentManager,
    *,
    store: object | None,
    failure: MirrorReconcileError | None = None,
) -> list[tuple[str, tuple[object, ...]]]:
    """Make the start path mirror-aware; the events record the pull and the materialize in the order they happen."""
    events: list[tuple[str, tuple[object, ...]]] = []

    async def reconcile(agent_id, agent_dir, durable, *, mode: MirrorMode, **_options: object) -> ReconcileReport:
        events.append(("pull", (agent_id, agent_dir, durable, mode)))
        if failure is not None:
            raise failure
        return ReconcileReport()

    async def materialize(agent_id):
        events.append(("materialize", (agent_id,)))
        return manager._agent_dir(agent_id)

    monkeypatch.setattr(agent_materialize, "durable_store", lambda: store)
    monkeypatch.setattr(agent_materialize, "reconcile_agent", reconcile)
    monkeypatch.setattr(manager, "_materialize_agent_dir", materialize)
    return events


async def test_rehydration_pulls_the_mirror_before_it_materializes(env, monkeypatch):
    # Given a mirror-active start of an agent that has a guest.
    manager, docker, agent = env
    store = object()
    events = mirror_active(monkeypatch, manager, store=store)
    # When the guest is replaced.
    result = await manager.replace_container(agent)
    # Then storage is pulled in pull-only mode into the guest directory before the directory is materialized.
    assert result == agent.container_id
    assert events == [
        ("pull", (agent.id, manager._agent_dir(agent.id), store, MirrorMode.PULL_ONLY)),
        ("materialize", (agent.id,)),
    ]
    assert len(docker.runs) == 1


@pytest.mark.parametrize(("guest_present", "code"), [(True, "runtime_failed"), (False, "configuration_failed")])
async def test_a_pull_that_stopped_early_without_a_base_fails_the_start(env, monkeypatch, guest_present, code):
    # Given a pull that cannot finish for a directory with no valid mirror base.
    manager, docker, agent = env
    if not guest_present:
        docker.guests.clear()
        agent.container_id = None
    failure = MirrorReconcileError("storage unreachable", had_base=False)
    events = mirror_active(monkeypatch, manager, store=object(), failure=failure)
    # When the guest is replaced.
    with pytest.raises(GuestStartPreflightError) as refused:
        await manager.replace_container(agent)
    # Then the start fails as a preparation error, before the directory is materialized or a guest is created.
    assert refused.value.code == code
    assert [name for name, _ in events] == ["pull"]
    assert docker.runs == []


async def test_a_pull_that_stopped_early_with_a_base_does_not_stop_the_start(env, monkeypatch):
    manager, docker, agent = env
    failure = MirrorReconcileError("storage slow", had_base=True)
    events = mirror_active(monkeypatch, manager, store=object(), failure=failure)

    result = await manager.replace_container(agent)

    assert result == agent.container_id
    assert [name for name, _ in events] == ["pull", "materialize"]
    assert len(docker.runs) == 1


async def test_without_a_durable_store_the_start_never_pulls(env, monkeypatch):
    manager, docker, agent = env
    events = mirror_active(monkeypatch, manager, store=None)

    result = await manager.replace_container(agent)

    assert result == agent.container_id
    assert [name for name, _ in events] == ["materialize"]
    assert len(docker.runs) == 1
