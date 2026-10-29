import builtins
import sys
from collections.abc import AsyncGenerator, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from typing import Unpack
from uuid import UUID, uuid4

import anyio
import pytest
from pywhales import DockerClient
from pywhales.components.container import cli_wrapper as container_cli
from pywhales.exceptions import NoSuchContainer

from app.records.agent import AgentRecord
from app.services import agent_orphans, openclaw_update_gate
from app.services.agent_setup_lock import setup_workspace_lock
from builtin_keyword_fakes import IncludeAllKeyword

NOW = datetime(2026, 10, 22, tzinfo=UTC)
NETWORK = "reconciliation-test"


@dataclass
class Config:
    labels: dict[str, str]


@dataclass
class NetworkSettings:
    networks: dict[str, object]


@dataclass
class Guest:
    id: str
    config: Config
    network_settings: NetworkSettings
    created: datetime = NOW - timedelta(minutes=10)


def guest(agent_id: UUID, container_id: str) -> Guest:
    return Guest(container_id, Config({"maraclaw.agent_id": str(agent_id)}), NetworkSettings({NETWORK: {}}))


class Containers:
    def __init__(self, guests: builtins.list[Guest]) -> None:
        self.listed: builtins.list[Guest] = builtins.list(guests)
        self.inspected: dict[str, Guest] = {item.id: item for item in guests}
        self.calls: builtins.list[tuple[str, str]] = []

    def list(self, *, filters: Iterable[tuple[str, str]], **kwargs: Unpack[IncludeAllKeyword]) -> builtins.list[Guest]:
        return self.listed

    def inspect(self, reference: str) -> Guest:
        if reference not in self.inspected:
            raise NoSuchContainer(["docker", "inspect", reference], 1)
        return self.inspected[reference]

    def stop(self, reference: str, *, time: int) -> None:
        self.calls.append(("stop", reference))

    def remove(self, references: builtins.list[str]) -> None:
        self.calls.extend(("remove", reference) for reference in references)


@dataclass
class Sweep:
    agent: AgentRecord = field(
        default_factory=lambda: AgentRecord(id=uuid4(), name="A", creator_id=uuid4(), container_id="current")
    )
    active_run: str | None = None
    current: AgentRecord | None = field(init=False)
    containers: Containers = field(init=False)

    def __post_init__(self) -> None:
        self.current = replace(self.agent)
        self.containers = Containers([guest(self.agent.id, "duplicate")])
        self.containers.inspected["current"] = guest(self.agent.id, "current")

    async def get(self, _agent_id: UUID) -> AgentRecord | None:
        return replace(self.current) if self.current is not None else None

    async def get_many(self, _ids: list[UUID]) -> list[AgentRecord]:
        return [replace(self.agent)]

    async def gate_state(self, _agent_id: UUID) -> tuple[bool, UUID | None]:
        return self.active_run in {"pending", "cutover"}, None


@pytest.fixture
def sweep(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Sweep:
    sweep = Sweep()
    monkeypatch.setattr(
        agent_orphans,
        "agent_manager",
        SimpleNamespace(
            docker_client=SimpleNamespace(container=sweep.containers),
            _agent_dir=lambda agent_id: tmp_path / str(agent_id),
        ),
    )
    monkeypatch.setattr(agent_orphans, "get_settings", lambda: SimpleNamespace(DOCKER_NETWORK=NETWORK))
    monkeypatch.setattr(agent_orphans.agent_dao, "get_many", sweep.get_many)
    monkeypatch.setattr(agent_orphans.agent_dao, "get", sweep.get)
    monkeypatch.setattr(openclaw_update_gate.openclaw_update_notice_dao, "gate_state", sweep.gate_state)
    return sweep


async def test_sweep_emits_label_and_network_filters_through_the_real_wrapper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given the installed PyWhales container.list with only its process execution boundary stubbed.
    client = DockerClient(client_call=[sys.executable])
    commands: list[list[str]] = []

    def run(command: Sequence[str | Path]) -> str:
        commands.append([str(argument) for argument in command])
        return ""

    monkeypatch.setattr(container_cli, "run", run)
    monkeypatch.setattr(
        agent_orphans,
        "agent_manager",
        SimpleNamespace(docker_client=client, _agent_dir=lambda agent_id: tmp_path / str(agent_id)),
    )
    monkeypatch.setattr(agent_orphans, "get_settings", lambda: SimpleNamespace(DOCKER_NETWORK=NETWORK))
    # When the sweep lists an empty Docker inventory.
    await agent_orphans.sweep_orphan_containers(NOW)
    # Then the wrapper accepts the request and Docker receives --all with both filters.
    (command,) = commands
    filters = [value for flag, value in pairwise(command) if flag == "--filter"]
    assert sorted(filters) == ["label=maraclaw.agent_id", f"network={NETWORK}"]
    assert "--all" in command


async def test_older_owned_duplicate_is_removed_when_the_current_guest_is_known(sweep: Sweep) -> None:
    # Given an old owned duplicate on the required network and a separate recorded guest.
    # When reconciliation inspects the candidate and recorded guest.
    await agent_orphans.sweep_orphan_containers(NOW)
    # Then only the proven obsolete duplicate is stopped and removed.
    assert sweep.containers.calls == [("stop", "duplicate"), ("remove", "duplicate")]


@pytest.mark.parametrize("reference", ["duplicate", "short-recorded-id"])
async def test_current_recorded_guest_is_kept_even_with_a_short_id(sweep: Sweep, reference: str) -> None:
    # Given a list candidate that resolves to the currently recorded Docker identity.
    assert sweep.current is not None
    sweep.current.container_id = reference
    sweep.containers.inspected[reference] = sweep.containers.inspected["duplicate"]
    # When reconciliation sees a different stale prefetched reference.
    await agent_orphans.sweep_orphan_containers(NOW)
    # Then full Docker identity comparison protects the current guest.
    assert sweep.containers.calls == []


@pytest.mark.parametrize("active_run", ["pending", "cutover"])
async def test_active_upgrade_keeps_older_duplicates(sweep: Sweep, active_run: str) -> None:
    # Given an active run, even if its cutover worker is not holding the flock yet.
    sweep.active_run = active_run
    # When reconciliation finds an otherwise removable old guest.
    await agent_orphans.sweep_orphan_containers(NOW)
    # Then a pending/cutover guest is not treated as an ordinary duplicate.
    assert sweep.containers.calls == []


@pytest.mark.parametrize("reference", [None, "", "absent-current"])
async def test_duplicate_is_kept_without_a_resolvable_current_identity(sweep: Sweep, reference: str | None) -> None:
    # Given an existing agent whose current Docker identity cannot be established.
    assert sweep.current is not None
    sweep.current.container_id = reference
    # When an older same-label guest appears in the scan.
    await agent_orphans.sweep_orphan_containers(NOW)
    # Then absence of identity evidence never licenses deletion.
    assert sweep.containers.calls == []


@pytest.mark.parametrize("changed", ["owner", "network", "age", "identity", "label-format"])
async def test_candidate_is_revalidated_after_the_batch_scan(sweep: Sweep, changed: str) -> None:
    # Given a safe-looking batch result whose Docker inspect now contradicts it.
    candidate = sweep.containers.inspected["duplicate"]
    changed_guests: dict[str, Guest] = {
        "owner": replace(candidate, config=Config({"maraclaw.agent_id": str(uuid4())})),
        "network": replace(candidate, network_settings=NetworkSettings({"foreign": {}})),
        "age": replace(candidate, created=NOW - timedelta(seconds=119)),
        "identity": replace(candidate, id="different-container"),
        "label-format": replace(candidate, config=Config({"maraclaw.agent_id": sweep.agent.id.hex})),
    }
    sweep.containers.inspected["duplicate"] = changed_guests[changed]
    # When reconciliation rechecks inside the workspace flock.
    await agent_orphans.sweep_orphan_containers(NOW)
    # Then stale ownership/network/age/identity evidence cannot authorize cleanup.
    assert sweep.containers.calls == []


async def test_foreign_current_reference_does_not_abort_later_owned_duplicate(sweep: Sweep) -> None:
    # Given a first agent pointing at a foreign current guest and a second valid duplicate.
    other = replace(sweep.agent, id=uuid4(), container_id="other-current")
    sweep.containers.inspected["current"] = guest(uuid4(), "current")
    sweep.containers.listed.append(guest(other.id, "other-duplicate"))
    sweep.containers.inspected["other-duplicate"] = sweep.containers.listed[-1]
    sweep.containers.inspected["other-current"] = guest(other.id, "other-current")

    async def get_many(_ids: list[UUID]) -> list[AgentRecord]:
        return [sweep.agent, other]

    async def get(agent_id: UUID) -> AgentRecord | None:
        return sweep.current if agent_id == sweep.agent.id else other

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(agent_orphans.agent_dao, "get_many", get_many)
        patch.setattr(agent_orphans.agent_dao, "get", get)
        # When both agents are reconciled in one pass.
        await agent_orphans.sweep_orphan_containers(NOW)
    # Then the foreign refusal is isolated and the later duplicate still converges.
    assert sweep.containers.calls == [("stop", "other-duplicate"), ("remove", "other-duplicate")]


@pytest.mark.parametrize("age_seconds", [0, 119])
async def test_new_guests_are_kept_by_the_grace_period(sweep: Sweep, age_seconds: int) -> None:
    # Given a newly launched guest that is not old enough to be an orphan.
    sweep.containers.listed[0].created = NOW - timedelta(seconds=age_seconds)
    # When reconciliation lists it before publication.
    await agent_orphans.sweep_orphan_containers(NOW)
    # Then the existing two-minute creation grace remains in force.
    assert sweep.containers.calls == []


@pytest.mark.parametrize("missing", ["config", "network_settings", "created"])
async def test_missing_orphan_metadata_never_authorizes_removal(sweep: Sweep, missing: str) -> None:
    setattr(sweep.containers.listed[0], missing, None)
    await agent_orphans.sweep_orphan_containers(NOW)
    assert sweep.containers.calls == []


async def test_unpublished_guest_is_kept_when_start_publishes_under_the_flock(
    sweep: Sweep, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given an older unpublished guest whose start still holds the workspace flock.
    waiting = anyio.Event()

    @asynccontextmanager
    async def observed_lock(path: Path) -> AsyncGenerator[None]:
        waiting.set()
        async with setup_workspace_lock(path):
            yield

    monkeypatch.setattr(agent_orphans, "setup_workspace_lock", observed_lock)
    # When start publishes that guest before the sweep can revalidate its stale batch.
    with anyio.fail_after(3):
        async with (
            anyio.create_task_group() as tasks,
            setup_workspace_lock(agent_orphans.agent_manager._agent_dir(sweep.agent.id)),
        ):
            tasks.start_soon(agent_orphans.sweep_orphan_containers, NOW)
            await waiting.wait()
            sweep.current = replace(sweep.agent, container_id="duplicate", status="running")
    # Then even a start longer than the grace period cannot lose its published guest.
    assert sweep.containers.calls == []


@pytest.mark.parametrize("active_run", ["pending", "cutover"])
async def test_parked_upgrade_writer_is_excluded_after_the_sweep_waits_for_the_flock(
    sweep: Sweep, monkeypatch: pytest.MonkeyPatch, active_run: str
) -> None:
    # Given a sweep queued behind a writer before its active run is visible.
    waiting = anyio.Event()

    @asynccontextmanager
    async def observed_lock(path: Path) -> AsyncGenerator[None]:
        waiting.set()
        async with setup_workspace_lock(path):
            yield

    monkeypatch.setattr(agent_orphans, "setup_workspace_lock", observed_lock)
    # When a pending/cutover writer parks and releases the flock while its run stays active.
    with anyio.fail_after(3):
        async with (
            anyio.create_task_group() as tasks,
            setup_workspace_lock(agent_orphans.agent_manager._agent_dir(sweep.agent.id)),
        ):
            tasks.start_soon(agent_orphans.sweep_orphan_containers, NOW)
            await waiting.wait()
            sweep.active_run = active_run
    # Then a free flock alone never authorizes deleting upgrade-owned duplicates.
    assert sweep.containers.calls == []
