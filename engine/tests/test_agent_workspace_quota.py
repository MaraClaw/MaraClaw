"""Per-agent workspace disk soft quota: measurement, policy, refusal mapping and enforcement points, with fakes."""

import os
import subprocess
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from pywhales import DockerClient

from app import main
from app.db import session
from app.db.connection import DbConnection
from app.db.session import get_connection
from app.records.agent import AgentRecord
from app.records.agent_workspace_usage import AgentWorkspaceUsageRecord
from app.records.organization_plan import OrganizationPlanId
from app.records.personal_burst_input import ReceiptKey
from app.records.user import UserRecord
from app.records.whatsapp_personal import PersonalRoute
from app.services import agent_resource_pressure as pressure, agent_workspace_quota as quota
from app.services.agent_workspace_quota import WorkspaceDiskQuotaExceeded
from app.services.channels import personal_burst_admission
from app.services.channels.personal_burst_original import PersonalOriginal
from app.services.organization_plans import AmbiguousOrganizationTierError
from app.services.quota_guard import QuotaExceededError

GIB = 1024**3
PRO_LIMIT = 3_221_225_472


def _write(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def test_walk_sums_nested_regular_files(tmp_path: Path):
    _ = _write(tmp_path / "a.txt", 10)
    _ = _write(tmp_path / "deep" / "er" / "b.bin", 20)
    (tmp_path / "empty").mkdir()

    assert quota.measure_directory_bytes(tmp_path) == 30


def test_walk_never_follows_symlinks(tmp_path: Path):
    outside = tmp_path / "outside"
    _ = _write(outside / "huge.bin", 1000)
    root = tmp_path / "agent"
    _ = _write(root / "own.txt", 5)
    (root / "dir-link").symlink_to(outside, target_is_directory=True)
    (root / "file-link").symlink_to(outside / "huge.bin")
    (root / "sub").mkdir()
    (root / "sub" / "loop").symlink_to(root, target_is_directory=True)

    assert quota.measure_directory_bytes(root) == 5


def test_walk_counts_a_hard_linked_file_once(tmp_path: Path):
    original = _write(tmp_path / "one.bin", 100)
    (tmp_path / "sub").mkdir()
    os.link(original, tmp_path / "two.bin")
    os.link(original, tmp_path / "sub" / "three.bin")
    _ = _write(tmp_path / "other.bin", 7)

    assert quota.measure_directory_bytes(tmp_path) == 107


def test_missing_or_unusable_root_is_empty(tmp_path: Path):
    target = _write(tmp_path / "file.bin", 9)
    link = tmp_path / "root-link"
    link.symlink_to(tmp_path, target_is_directory=True)

    assert quota.measure_directory_bytes(tmp_path / "missing") == 0
    assert quota.measure_directory_bytes(target) == 0
    assert quota.measure_directory_bytes(link) == 0


@pytest.mark.parametrize("error", [FileNotFoundError, NotADirectoryError, PermissionError])
def test_walk_skips_a_directory_that_vanishes_or_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: type[OSError]
):
    _ = _write(tmp_path / "kept.bin", 11)
    _ = _write(tmp_path / "gone" / "lost.bin", 500)
    real_scandir = os.scandir

    def flaky(path: str | os.PathLike[str]) -> object:
        if os.fspath(path).endswith("gone"):
            raise error(path)
        return real_scandir(path)

    monkeypatch.setattr(quota.os, "scandir", flaky)

    assert quota.measure_directory_bytes(tmp_path) == 11


def _client() -> DockerClient:
    return cast("DockerClient", SimpleNamespace(docker_cmd=["docker", "--host", "unix:///x.sock"]))


def _inspect(
    monkeypatch: pytest.MonkeyPatch, *, stdout: str = "", returncode: int = 0, raises: Exception | None = None
):
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((command, kwargs))
        if raises is not None:
            raise raises
        return SimpleNamespace(returncode=returncode, stdout=stdout)

    monkeypatch.setattr(quota.subprocess, "run", run)
    return calls


def test_layer_size_runs_the_local_cli_without_a_shell(monkeypatch: pytest.MonkeyPatch):
    calls = _inspect(monkeypatch, stdout="5505024\n")

    assert quota.measure_layer_bytes(_client(), "abc123", 7.5) == 5505024

    ((command, kwargs),) = calls
    assert command == [
        "docker",
        "--host",
        "unix:///x.sock",
        "container",
        "inspect",
        "--size",
        "--format",
        "{{.SizeRw}}",
        "abc123",
    ]
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["timeout"] == 7.5
    assert kwargs["check"] is False
    assert "shell" not in kwargs


@pytest.mark.parametrize(
    ("stdout", "returncode", "expected"),
    [
        ("0\n", 0, 0),
        ("<nil>\n", 0, None),
        ("garbage", 0, None),
        ("-5", 0, None),
        ("12abc", 0, None),
        ("1.5", 0, None),
        ("", 0, None),
        ("\u0663", 0, None),
        ("5505024", 1, None),
    ],
)
def test_layer_size_parses_only_a_non_negative_integer_from_a_clean_exit(
    monkeypatch: pytest.MonkeyPatch, stdout: str, returncode: int, expected: int | None
):
    _ = _inspect(monkeypatch, stdout=stdout, returncode=returncode)

    assert quota.measure_layer_bytes(_client(), "abc123", 5) == expected


@pytest.mark.parametrize(
    "error", [subprocess.TimeoutExpired("docker", 5), FileNotFoundError("docker"), PermissionError("docker")]
)
def test_layer_size_is_unavailable_when_the_cli_cannot_be_asked(monkeypatch: pytest.MonkeyPatch, error: Exception):
    _ = _inspect(monkeypatch, raises=error)

    with pytest.raises(quota.LayerReadUnavailableError, match=type(error).__name__):
        _ = quota.measure_layer_bytes(_client(), "abc123", 5)


def test_layer_size_does_not_launch_with_an_exhausted_deadline(monkeypatch: pytest.MonkeyPatch):
    calls = _inspect(monkeypatch, stdout="1")

    assert quota.measure_layer_bytes(_client(), "abc123", 0) is None
    assert calls == []


@dataclass
class World:
    """Fake collaborators for ``measure_agent`` whose usage store applies the stored-row rules in memory."""

    rows: dict[UUID, AgentWorkspaceUsageRecord] = field(default_factory=dict)
    walks: list[Path] = field(default_factory=list)
    inspects: list[str] = field(default_factory=list)
    open_connections: int = 0
    connections_during_walk: list[int] = field(default_factory=list)
    dir_bytes: int = 0
    layer_bytes: int | None = 0
    layer_error: Exception | None = None
    notify: AsyncMock = field(default_factory=AsyncMock)
    tier: AsyncMock = field(default_factory=lambda: AsyncMock(return_value=OrganizationPlanId.PRO))
    pressure: list[tuple[AgentRecord, OrganizationPlanId, int, int, int]] = field(default_factory=list)

    async def record_pressure(
        self, agent: AgentRecord, tier: OrganizationPlanId, *, dir_bytes: int, layer_bytes: int, limit_bytes: int
    ) -> None:
        self.pressure.append((agent, tier, dir_bytes, layer_bytes, limit_bytes))

    async def record_measurement(
        self, agent_id: UUID, *, dir_bytes: int, layer_bytes: int, limit_bytes: int, started: datetime
    ) -> AgentWorkspaceUsageRecord | None:
        previous = self.rows.get(agent_id)
        if previous is not None and previous.measured_at is not None and previous.measured_at > started:
            return None
        over = dir_bytes + layer_bytes > limit_bytes
        row = AgentWorkspaceUsageRecord(
            agent_id,
            dir_bytes,
            layer_bytes,
            limit_bytes,
            measured_at=started,
            over_since=(previous.over_since if previous and previous.over_since else started) if over else None,
            notified_at=previous.notified_at if over and previous else None,
        )
        self.rows[agent_id] = row
        return row

    async def claim_notification(self, agent_id: UUID) -> AgentWorkspaceUsageRecord | None:
        row = self.rows.get(agent_id)
        if row is None or row.over_since is None or row.notified_at is not None:
            return None
        row.notified_at = datetime.now(UTC)
        return row

    async def delete(self, *, record_id: UUID) -> AgentWorkspaceUsageRecord | None:
        return self.rows.pop(record_id, None)

    async def get(self, record_id: UUID) -> AgentWorkspaceUsageRecord | None:
        return self.rows.get(record_id)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> World:
    state = World()

    def walk(path: Path) -> int:
        state.walks.append(path)
        state.connections_during_walk.append(state.open_connections)
        return state.dir_bytes

    def inspect(_client: object, container_id: str, _timeout: float) -> int | None:
        state.inspects.append(container_id)
        if state.layer_error is not None:
            raise state.layer_error
        return state.layer_bytes

    @asynccontextmanager
    async def connection() -> AsyncGenerator[None]:
        state.open_connections += 1
        try:
            yield
        finally:
            state.open_connections -= 1

    dao = quota.agent_workspace_usage_dao
    monkeypatch.setattr(dao, "record_measurement", state.record_measurement)
    monkeypatch.setattr(dao, "claim_notification", state.claim_notification)
    monkeypatch.setattr(dao, "delete", state.delete)
    monkeypatch.setattr(dao, "get", state.get)
    monkeypatch.setattr(quota, "tier_for_tenant", state.tier)
    monkeypatch.setattr(quota, "send_notification", state.notify)
    monkeypatch.setattr(quota, "record_workspace_disk_pressure", state.record_pressure)
    monkeypatch.setattr(quota, "connection_ctx", connection)
    monkeypatch.setattr(quota, "measure_directory_bytes", walk)
    monkeypatch.setattr(quota, "measure_layer_bytes", inspect)
    monkeypatch.setattr(quota.agent_manager, "_agent_dir", lambda agent_id: tmp_path / str(agent_id))
    monkeypatch.setattr(quota.agent_manager, "reacquire_docker_client", lambda: SimpleNamespace(docker_cmd=["docker"]))
    return state


def _agent(*, tenant: bool = True, system: bool = False, container: str | None = "abc123") -> AgentRecord:
    return AgentRecord(
        id=uuid4(),
        name="Ops",
        creator_id=uuid4(),
        tenant_id=uuid4() if tenant else None,
        is_system=system,
        container_id=container,
    )


async def test_an_agent_under_its_allowance_is_recorded_without_an_alert(world: World, tmp_path: Path):
    agent = _agent()
    world.dir_bytes, world.layer_bytes = GIB, GIB // 2

    await quota.measure_agent(agent)

    row = world.rows[agent.id]
    assert (row.dir_bytes, row.layer_bytes, row.limit_bytes, row.over_since) == (GIB, GIB // 2, PRO_LIMIT, None)
    assert world.walks == [tmp_path / str(agent.id)]
    assert world.inspects == ["abc123"]
    world.notify.assert_not_awaited()


async def test_exactly_the_allowance_is_not_over(world: World):
    agent = _agent()
    world.dir_bytes, world.layer_bytes = PRO_LIMIT - 1, 1

    await quota.measure_agent(agent)

    assert world.rows[agent.id].over_since is None


async def test_the_walk_and_the_inspect_hold_no_database_connection(world: World):
    world.dir_bytes = 4 * GIB

    await quota.measure_agent(_agent())

    assert world.connections_during_walk == [0]
    assert world.open_connections == 0


async def test_an_over_agent_alerts_its_creator_once_with_a_reference_and_no_agent_recipient(world: World):
    agent = _agent()
    world.dir_bytes, world.layer_bytes = 2 * GIB, 2 * GIB

    await quota.measure_agent(agent)

    world.notify.assert_awaited_once()
    call = world.notify.await_args
    assert call is not None
    kwargs = call.kwargs
    assert kwargs["user_id"] == agent.creator_id
    assert kwargs["ref_id"] == agent.id
    assert kwargs["notification_kind"].value == "system_alert"
    assert "agent_id" not in kwargs
    assert "4.00 GiB" in kwargs["body"]
    assert "3.00 GiB" in kwargs["body"]
    assert "files 2.00 GiB, container 2.00 GiB" in kwargs["body"]
    assert kwargs["body"].endswith("Stop and start the agent to clear its container's temporary data.")
    assert len(kwargs["title"]) <= 200
    assert world.rows[agent.id].over_since is not None
    assert world.rows[agent.id].notified_at is not None


async def test_an_alert_for_files_over_the_allowance_says_to_delete_files(world: World):
    world.dir_bytes, world.layer_bytes = 4 * GIB, GIB

    await quota.measure_agent(_agent())

    call = world.notify.await_args
    assert call is not None
    assert call.kwargs["body"].endswith("Delete files to continue.")


async def test_a_second_pass_over_the_allowance_does_not_alert_again(world: World):
    agent = _agent()
    world.dir_bytes = 4 * GIB
    await quota.measure_agent(agent)
    first = world.rows[agent.id].over_since

    await quota.measure_agent(agent)

    world.notify.assert_awaited_once()
    assert world.rows[agent.id].over_since == first


async def test_dropping_under_clears_the_block_and_a_later_overage_alerts_again(world: World):
    agent = _agent()
    world.dir_bytes = 4 * GIB
    await quota.measure_agent(agent)

    world.dir_bytes = GIB
    await quota.measure_agent(agent)

    row = world.rows[agent.id]
    assert (row.over_since, row.notified_at) == (None, None)
    world.dir_bytes = 4 * GIB
    await quota.measure_agent(agent)
    assert world.notify.await_count == 2


@pytest.mark.parametrize("agent", [_agent(system=True), _agent(tenant=False)], ids=["system", "tenantless"])
async def test_agents_without_an_allowance_lose_their_row_and_are_never_walked(world: World, agent: AgentRecord):
    world.rows[agent.id] = AgentWorkspaceUsageRecord(agent.id, 1, 1, 1, over_since=datetime.now(UTC))

    await quota.measure_agent(agent)

    assert world.rows == {}
    assert world.walks == []
    world.tier.assert_not_awaited()


async def test_an_untiered_organization_loses_its_row_and_is_never_walked(world: World):
    agent = _agent()
    world.tier.return_value = None
    world.rows[agent.id] = AgentWorkspaceUsageRecord(agent.id, 1, 1, 1, over_since=datetime.now(UTC))

    await quota.measure_agent(agent)

    assert world.rows == {}
    assert world.walks == []
    world.notify.assert_not_awaited()


async def test_an_ambiguous_tier_link_is_skipped_and_leaves_the_row_alone(world: World):
    agent = _agent()
    world.tier.side_effect = AmbiguousOrganizationTierError(uuid4())
    stored = AgentWorkspaceUsageRecord(agent.id, 1, 1, 1, over_since=datetime.now(UTC))
    world.rows[agent.id] = stored

    await quota.measure_agent(agent)

    assert world.rows == {agent.id: stored}
    assert world.walks == []


async def test_an_unreadable_layer_with_nothing_stored_counts_as_zero(world: World):
    world.dir_bytes, world.layer_bytes = GIB, None
    agent = _agent()

    await quota.measure_agent(agent)

    assert world.rows[agent.id].layer_bytes == 0
    assert world.inspects == ["abc123"]


async def test_an_unreadable_layer_keeps_the_stored_size_and_a_missing_container_is_not_inspected(world: World):
    world.dir_bytes, world.layer_bytes = GIB, None
    unknown = _agent()
    stopped = _agent(container=None)
    for agent in (unknown, stopped):
        world.rows[agent.id] = AgentWorkspaceUsageRecord(agent.id, GIB, GIB // 2, PRO_LIMIT)

    await quota.measure_agent(unknown)
    await quota.measure_agent(stopped)

    assert world.rows[unknown.id].layer_bytes == GIB // 2
    assert world.rows[stopped.id].layer_bytes == 0
    assert world.inspects == ["abc123"]


async def test_an_unreadable_layer_does_not_lift_the_block_or_alert_again(world: World):
    agent = _agent()
    world.dir_bytes, world.layer_bytes = GIB, 3 * GIB
    await quota.measure_agent(agent)
    first = world.rows[agent.id].over_since
    assert first is not None

    world.layer_bytes = None
    await quota.measure_agent(agent)

    row = world.rows[agent.id]
    assert (row.layer_bytes, row.over_since) == (3 * GIB, first)
    world.notify.assert_awaited_once()


async def test_without_a_docker_client_only_the_directory_counts(world: World, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(quota.agent_manager, "reacquire_docker_client", lambda: None)
    agent = _agent()
    world.dir_bytes = GIB

    await quota.measure_agent(agent)

    assert world.inspects == []
    assert world.rows[agent.id].layer_bytes == 0


async def test_without_a_docker_client_the_stored_layer_size_is_kept(world: World, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(quota.agent_manager, "reacquire_docker_client", lambda: None)
    agent = _agent()
    world.rows[agent.id] = AgentWorkspaceUsageRecord(agent.id, GIB, GIB, PRO_LIMIT)
    world.dir_bytes = GIB

    await quota.measure_agent(agent)

    assert world.inspects == []
    assert world.rows[agent.id].layer_bytes == GIB


async def test_after_docker_stops_answering_the_rest_of_the_pass_keeps_stored_layer_sizes(
    world: World, monkeypatch: pytest.MonkeyPatch
):
    warnings: list[tuple[object, ...]] = []
    monkeypatch.setattr(quota, "logger", SimpleNamespace(warning=lambda *args: warnings.append(args)))
    first, second, third = _agent(container="c1"), _agent(container="c2"), _agent(container="c3")
    for agent in (second, third):
        world.rows[agent.id] = AgentWorkspaceUsageRecord(agent.id, GIB, GIB // 2, PRO_LIMIT)
    world.dir_bytes, world.layer_error = GIB, quota.LayerReadUnavailableError("TimeoutExpired")
    layers = quota.LayerReads()

    for agent in (first, second, third):
        await quota.measure_agent(agent, layers)

    assert world.inspects == ["c1"]
    assert layers.unavailable
    assert [world.rows[agent.id].layer_bytes for agent in (first, second, third)] == [0, GIB // 2, GIB // 2]
    assert len(warnings) == 1


async def test_the_layer_read_skip_ends_with_the_pass(world: World):
    agent = _agent()
    world.layer_error = quota.LayerReadUnavailableError("TimeoutExpired")
    layers = quota.LayerReads()
    await quota.measure_agent(agent, layers)
    world.layer_error, world.layer_bytes = None, GIB

    await quota.measure_agent(agent, layers)
    assert (world.inspects, world.rows[agent.id].layer_bytes) == (["abc123"], 0)

    await quota.measure_agent(agent, quota.LayerReads())
    assert (world.inspects, world.rows[agent.id].layer_bytes) == (["abc123", "abc123"], GIB)


async def test_an_answer_without_a_size_does_not_stop_the_rest_of_the_pass_reading_layers(world: World):
    world.layer_bytes = None
    layers = quota.LayerReads()

    for container in ("c1", "c2"):
        await quota.measure_agent(_agent(container=container), layers)

    assert world.inspects == ["c1", "c2"]
    assert not layers.unavailable


class Clock:
    """Stands in for ``datetime`` in the quota module: ``now`` replays the scripted instants, one per measurement."""

    def __init__(self, *instants: datetime) -> None:
        self.instants: list[datetime] = list(instants)

    def now(self, _tz: object = None) -> datetime:
        return self.instants.pop(0)


EARLIER = datetime(2026, 11, 14, 10, 0, 0, tzinfo=UTC)
LATER = datetime(2026, 11, 14, 10, 0, 5, tzinfo=UTC)


async def test_an_older_started_over_quota_write_landing_late_leaves_the_newer_under_quota_row(
    world: World, monkeypatch: pytest.MonkeyPatch
):
    # The worker's walk starts first and is slow; the delete's re-measure starts later and lands first.
    monkeypatch.setattr(quota, "datetime", Clock(LATER, EARLIER))
    agent = _agent()
    world.dir_bytes = GIB
    await quota.measure_agent(agent)
    world.dir_bytes = 4 * GIB

    await quota.measure_agent(agent)

    row = world.rows[agent.id]
    assert (row.dir_bytes, row.measured_at, row.over_since, row.notified_at) == (GIB, LATER, None, None)
    await quota.ensure_workspace_within_quota(agent)
    world.notify.assert_not_awaited()
    assert [sample[2] for sample in world.pressure] == [GIB]


async def test_an_older_started_under_quota_write_never_lifts_a_newer_block_or_alerts_again(
    world: World, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(quota, "datetime", Clock(LATER, EARLIER))
    agent = _agent()
    world.dir_bytes = 4 * GIB
    await quota.measure_agent(agent)
    world.dir_bytes = GIB

    await quota.measure_agent(agent)

    row = world.rows[agent.id]
    assert (row.dir_bytes, row.measured_at) == (4 * GIB, LATER)
    assert row.over_since is not None
    assert row.notified_at is not None
    world.notify.assert_awaited_once()
    assert [sample[2] for sample in world.pressure] == [4 * GIB]


async def test_each_stored_measurement_feeds_the_pressure_episodes(world: World):
    agent = _agent()
    world.dir_bytes, world.layer_bytes = GIB, GIB // 2

    await quota.measure_agent(agent)

    assert world.pressure == [(agent, OrganizationPlanId.PRO, GIB, GIB // 2, PRO_LIMIT)]


async def test_agents_without_an_allowance_never_reach_the_pressure_episodes(world: World):
    world.tier.return_value = None

    await quota.measure_agent(_agent())
    await quota.measure_agent(_agent(system=True))
    await quota.measure_agent(_agent(tenant=False))

    assert world.pressure == []


async def test_a_failing_pressure_store_never_breaks_the_quota_pass(world: World, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(quota, "record_workspace_disk_pressure", pressure.record_workspace_disk_pressure)
    monkeypatch.setattr(
        pressure.agent_resource_pressure_episode_dao, "record_pressure", AsyncMock(side_effect=OSError("database down"))
    )
    agent = _agent()
    world.dir_bytes = 4 * GIB

    await quota.measure_agent(agent)

    assert world.rows[agent.id].over_since is not None
    world.notify.assert_awaited_once()


@pytest.mark.parametrize(
    ("tier", "limit"),
    [
        (OrganizationPlanId.PRO, 3_221_225_472),
        (OrganizationPlanId.MAX, 5 * GIB),
        (OrganizationPlanId.ULTRA, 8 * GIB),
    ],
)
async def test_the_stored_limit_is_the_current_tier_allowance(world: World, tier: OrganizationPlanId, limit: int):
    agent = _agent()
    world.tier.return_value = tier

    await quota.measure_agent(agent)

    assert world.rows[agent.id].limit_bytes == limit


def _over(agent: AgentRecord, used: int = 4 * GIB, limit: int = PRO_LIMIT) -> AgentWorkspaceUsageRecord:
    return AgentWorkspaceUsageRecord(agent.id, used, 0, limit, over_since=datetime.now(UTC))


async def test_an_over_agent_refuses_new_work_with_the_allowance_in_the_message(world: World):
    agent = _agent()
    world.rows[agent.id] = AgentWorkspaceUsageRecord(
        agent.id, 2 * GIB, 2 * GIB, PRO_LIMIT, over_since=datetime.now(UTC)
    )

    with pytest.raises(WorkspaceDiskQuotaExceeded) as refused:
        await quota.ensure_workspace_within_quota(agent)

    assert isinstance(refused.value, QuotaExceededError)
    assert refused.value.quota_type == "agent_workspace_disk"
    assert (refused.value.used_bytes, refused.value.limit_bytes) == (4 * GIB, PRO_LIMIT)
    assert (refused.value.dir_bytes, refused.value.layer_bytes) == (2 * GIB, 2 * GIB)
    assert refused.value.message == (
        "This agent's workspace is using 4.00 GiB of its 3.00 GiB disk allowance. "
        "Stop and start the agent to clear its container's temporary data."
    )


@pytest.mark.parametrize(
    ("dir_bytes", "layer_bytes", "remedy"),
    [
        pytest.param(4 * GIB, 0, "Delete files to continue.", id="files-over"),
        pytest.param(4 * GIB, GIB, "Delete files to continue.", id="files-over-and-layer"),
        pytest.param(PRO_LIMIT + 1, 0, "Delete files to continue.", id="files-one-byte-over"),
        pytest.param(PRO_LIMIT, 1, "Stop and start the agent to clear its container's temporary data.", id="files-fit"),
        pytest.param(GIB, 2 * GIB, "Stop and start the agent to clear its container's temporary data.", id="layer"),
    ],
)
def test_the_refusal_names_the_remedy_for_its_cause(dir_bytes: int, layer_bytes: int, remedy: str):
    refused = WorkspaceDiskQuotaExceeded(dir_bytes, layer_bytes, PRO_LIMIT)

    assert refused.message.endswith(f"disk allowance. {remedy}")
    assert len(refused.message) <= 180


async def test_an_agent_without_a_block_is_admitted(world: World):
    under, unmeasured = _agent(), _agent()
    world.rows[under.id] = AgentWorkspaceUsageRecord(under.id, 1, 1, PRO_LIMIT)

    await quota.ensure_workspace_within_quota(under)
    await quota.ensure_workspace_within_quota(unmeasured)


@pytest.mark.parametrize("agent", [_agent(system=True), _agent(tenant=False)], ids=["system", "tenantless"])
async def test_system_and_tenantless_agents_are_never_read_or_refused(
    world: World, monkeypatch: pytest.MonkeyPatch, agent: AgentRecord
):
    world.rows[agent.id] = _over(agent)
    read = AsyncMock()
    monkeypatch.setattr(quota.agent_workspace_usage_dao, "get", read)

    await quota.ensure_workspace_within_quota(agent)

    read.assert_not_awaited()


async def test_the_next_measurement_lifts_the_block(world: World):
    agent = _agent()
    world.rows[agent.id] = _over(agent)
    world.dir_bytes = GIB

    await quota.measure_agent(agent)

    await quota.ensure_workspace_within_quota(agent)


async def test_a_delete_re_measures_only_an_agent_that_is_blocked(world: World, monkeypatch: pytest.MonkeyPatch):
    measure = AsyncMock()
    monkeypatch.setattr(quota, "measure_agent", measure)
    free, blocked = _agent(), _agent()
    world.rows[blocked.id] = _over(blocked)

    await quota.refresh_workspace_usage_after_delete(free)
    measure.assert_not_awaited()
    await quota.refresh_workspace_usage_after_delete(blocked)

    measure.assert_awaited_once_with(blocked)


@pytest.mark.parametrize("failing", ["read", "measure"])
async def test_a_failed_re_measure_after_a_delete_never_raises(
    world: World, monkeypatch: pytest.MonkeyPatch, failing: str
):
    agent = _agent()
    world.rows[agent.id] = _over(agent)
    if failing == "read":
        monkeypatch.setattr(quota.agent_workspace_usage_dao, "get", AsyncMock(side_effect=RuntimeError("down")))
    else:
        monkeypatch.setattr(quota, "measure_agent", AsyncMock(side_effect=OSError("disk")))

    await quota.refresh_workspace_usage_after_delete(agent)


@pytest.mark.parametrize(
    ("row", "layer_caused"),
    [
        pytest.param(None, False, id="unmeasured"),
        pytest.param(AgentWorkspaceUsageRecord(uuid4(), GIB, GIB, PRO_LIMIT), False, id="under-allowance"),
        pytest.param(
            AgentWorkspaceUsageRecord(uuid4(), 4 * GIB, GIB, PRO_LIMIT, over_since=datetime.now(UTC)),
            False,
            id="files-over",
        ),
        pytest.param(
            AgentWorkspaceUsageRecord(uuid4(), GIB, 2 * GIB, PRO_LIMIT, over_since=datetime.now(UTC)),
            True,
            id="layer-over",
        ),
        pytest.param(
            AgentWorkspaceUsageRecord(uuid4(), PRO_LIMIT, 1, PRO_LIMIT, over_since=datetime.now(UTC)),
            True,
            id="files-exactly-the-allowance",
        ),
    ],
)
async def test_only_a_block_whose_files_fit_is_caused_by_the_layer(
    world: World, row: AgentWorkspaceUsageRecord | None, layer_caused: bool
):
    agent = _agent()
    if row is not None:
        world.rows[agent.id] = replace(row, agent_id=agent.id)

    assert await quota.layer_caused_block(agent) is layer_caused


async def test_a_re_measure_after_recreation_lifts_a_layer_caused_block_at_once(world: World):
    agent = _agent(container="new-guest")
    world.rows[agent.id] = AgentWorkspaceUsageRecord(
        agent.id, GIB, 2 * GIB, PRO_LIMIT, over_since=datetime.now(UTC), notified_at=datetime.now(UTC)
    )
    world.dir_bytes, world.layer_bytes = GIB, 4096

    await quota.remeasure_after_recreation(agent)

    await quota.ensure_workspace_within_quota(agent)
    row = world.rows[agent.id]
    assert (row.dir_bytes, row.layer_bytes, row.over_since, row.notified_at) == (GIB, 4096, None, None)
    assert world.inspects == ["new-guest"]
    assert [entry[2:] for entry in world.pressure] == [(GIB, 4096, PRO_LIMIT)]
    world.notify.assert_not_awaited()


async def test_a_re_measure_after_recreation_holds_no_connection_of_the_caller(
    world: World, monkeypatch: pytest.MonkeyPatch
):
    ambient = cast("DbConnection", object())
    seen: list[DbConnection | None] = []

    async def measure(_agent: AgentRecord, layers: object = None) -> None:
        seen.append(get_connection())

    monkeypatch.setattr(quota, "measure_agent", measure)
    token = session._conn_ctx.set(ambient)
    try:
        assert get_connection() is ambient
        await quota.remeasure_after_recreation(_agent())
    finally:
        session._conn_ctx.reset(token)

    assert seen == [None]


async def test_a_failed_re_measure_after_recreation_never_raises(world: World, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(quota, "measure_agent", AsyncMock(side_effect=OSError("disk")))

    await quota.remeasure_after_recreation(_agent())


async def test_the_quota_handler_is_registered_and_answers_507_with_the_allowance():
    handler = main.app.exception_handlers[WorkspaceDiskQuotaExceeded]
    scratch = FastAPI()
    scratch.add_exception_handler(Exception, main.app.exception_handlers[Exception])
    scratch.add_exception_handler(WorkspaceDiskQuotaExceeded, handler)

    @scratch.get("/refuse")
    async def refuse() -> None:
        raise WorkspaceDiskQuotaExceeded(2 * GIB, 2 * GIB, PRO_LIMIT)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=scratch), base_url="http://quota.test") as client:
        response = await client.get("/refuse")

    assert response.status_code == 507
    assert response.json() == {
        "detail": {
            "code": "workspace_disk_quota_exceeded",
            "message": "This agent's workspace is using 4.00 GiB of its 3.00 GiB disk allowance. "
            "Stop and start the agent to clear its container's temporary data.",
            "used_bytes": 4 * GIB,
            "limit_bytes": PRO_LIMIT,
            "dir_bytes": 2 * GIB,
            "layer_bytes": 2 * GIB,
        }
    }


@dataclass
class Savepoints:
    statements: list[str] = field(default_factory=list)

    async def execute(self, statement: str) -> None:
        self.statements.append(statement)


@pytest.mark.parametrize("over", [True, False])
async def test_a_personal_original_for_an_over_agent_is_a_quota_rejection_that_charges_nothing(
    monkeypatch: pytest.MonkeyPatch, over: bool
):
    savepoints = Savepoints()
    charge_sender, charge_agent = AsyncMock(), AsyncMock()
    ensure = AsyncMock(side_effect=WorkspaceDiskQuotaExceeded(4 * GIB, 0, PRO_LIMIT) if over else None)

    @asynccontextmanager
    async def connection() -> AsyncGenerator[Savepoints]:
        yield savepoints

    monkeypatch.setattr(personal_burst_admission, "connection_ctx", connection)
    monkeypatch.setattr(personal_burst_admission, "charge_conversation_quota", charge_sender)
    monkeypatch.setattr(personal_burst_admission, "charge_agent_daily_message", charge_agent)
    monkeypatch.setattr(personal_burst_admission, "ensure_workspace_within_quota", ensure)
    original = PersonalOriginal(
        agent=_agent(),
        sender=UserRecord(id=uuid4()),
        receipt=ReceiptKey(uuid4(), "whatsapp", "personal:1:n1"),
        text="hello",
        route=PersonalRoute(
            config_id=uuid4(), auth_epoch=uuid4(), generation=uuid4(), account_id="account", self_jid="self"
        ),
        shared_session_id=uuid4(),
        reply_session_id=uuid4(),
    )

    admitted = await personal_burst_admission._charge_sender_and_agent(original)

    assert admitted is not over
    ensure.assert_awaited_once_with(original.agent)
    assert charge_agent.await_count == (0 if over else 1)
    assert ("ROLLBACK TO SAVEPOINT personal_message_charges" in savepoints.statements) is over
