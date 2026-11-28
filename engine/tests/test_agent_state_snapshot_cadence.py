"""Automatic state backups follow the organization's current tier across scans and worker restarts."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from uuid import UUID, uuid4

import pytest

from app.dao.tenant_dao import tenant_dao
from app.records.organization_plan import OrganizationPlanId
from app.services import agent_state_snapshot_worker as worker
from app.services.agent_state_snapshot import new_snapshot_name, tenant_token
from app.services.agent_state_snapshot_tracker import SnapshotOutcome
from state_snapshot_fakes import write_file
from test_agent_state_snapshot_worker import World, world
from worker_progress_fixtures import WorkerHTTP, worker_http

__all__ = ["worker_http", "world"]


@dataclass(slots=True)
class SnapshotClock:
    at: datetime = datetime(2026, 11, 1, 12, tzinfo=UTC)

    def now(self, tz: tzinfo) -> datetime:
        return self.at.astimezone(tz)


@pytest.fixture
def wall_clock(monkeypatch: pytest.MonkeyPatch) -> SnapshotClock:
    clock = SnapshotClock()
    monkeypatch.setattr(worker, "datetime", clock)
    return clock


@pytest.fixture
def tiers(monkeypatch: pytest.MonkeyPatch) -> dict[UUID, OrganizationPlanId]:
    persisted: dict[UUID, OrganizationPlanId] = {}

    async def tiers_for(ids: Sequence[UUID]) -> dict[UUID, OrganizationPlanId]:
        return {tenant_id: persisted[tenant_id] for tenant_id in ids if tenant_id in persisted}

    monkeypatch.setattr(tenant_dao, "tiers_for", tiers_for)
    return persisted


def start_scan(world: World, worker_http: WorkerHTTP, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker.agent_manager, "docker_client", world.docker)
    monkeypatch.setattr(worker, "sleep", worker_http.pacing.wait)
    _ = worker_http.start_loop(
        "agent_state_snapshot",
        worker.start_agent_state_snapshot_worker(),
        budget_seconds=worker.progress_budget_seconds(),
    )


@pytest.mark.parametrize(
    ("tier", "days"), [(OrganizationPlanId.PRO, 15), (OrganizationPlanId.MAX, 7), (OrganizationPlanId.ULTRA, 1)]
)
async def test_backup_is_due_at_the_tier_boundary_after_a_restart(
    world: World,
    worker_http: WorkerHTTP,
    monkeypatch: pytest.MonkeyPatch,
    wall_clock: SnapshotClock,
    tiers: dict[UUID, OrganizationPlanId],
    tier: OrganizationPlanId,
    days: int,
) -> None:
    tenant_id = world.agent.tenant_id
    assert tenant_id is not None
    tiers[tenant_id] = tier
    assert await world.snapshot() == SnapshotOutcome.PUBLISHED
    [previous] = world.names()
    _ = write_file(world.agent_dir / ".openclaw-gateway-token", b"changed since the backup")
    due = wall_clock.at + timedelta(days=days)

    wall_clock.at = due - timedelta(microseconds=1)
    start_scan(world, worker_http, monkeypatch)
    await worker_http.pacing.paused()
    assert world.names() == [previous]
    assert len(world.docker.runs) == 1

    wall_clock.at = due
    await worker_http.pacing.step()
    [current] = world.names()
    assert current != previous
    assert len(world.docker.runs) == 2

    _ = write_file(world.agent_dir / ".openclaw-gateway-token", b"changed again since the second backup")
    wall_clock.at += timedelta(hours=1)
    await worker_http.pacing.step()
    assert world.names() == [current]
    assert len(world.docker.runs) == 2


async def test_upgrade_can_make_changed_state_due_on_the_next_scan(
    world: World,
    worker_http: WorkerHTTP,
    monkeypatch: pytest.MonkeyPatch,
    wall_clock: SnapshotClock,
    tiers: dict[UUID, OrganizationPlanId],
) -> None:
    tenant_id = world.agent.tenant_id
    assert tenant_id is not None
    tiers[tenant_id] = OrganizationPlanId.PRO
    start_scan(world, worker_http, monkeypatch)
    await worker_http.pacing.paused()
    [previous] = world.names()

    _ = write_file(world.agent_dir / ".openclaw-gateway-token", b"changed since the Pro backup")
    wall_clock.at += timedelta(days=1)
    await worker_http.pacing.step()
    assert world.names() == [previous]

    tiers[tenant_id] = OrganizationPlanId.ULTRA
    await worker_http.pacing.step()
    [current] = world.names()
    assert current != previous
    assert len(world.docker.runs) == 2


async def test_downgrade_postpones_a_due_backup_without_removing_the_recovery_point(
    world: World,
    worker_http: WorkerHTTP,
    monkeypatch: pytest.MonkeyPatch,
    wall_clock: SnapshotClock,
    tiers: dict[UUID, OrganizationPlanId],
) -> None:
    tenant_id = world.agent.tenant_id
    assert tenant_id is not None
    tiers[tenant_id] = OrganizationPlanId.ULTRA
    start_scan(world, worker_http, monkeypatch)
    await worker_http.pacing.paused()
    [previous] = world.names()

    _ = write_file(world.agent_dir / ".openclaw-gateway-token", b"changed since the Ultra backup")
    tiers[tenant_id] = OrganizationPlanId.PRO
    wall_clock.at += timedelta(days=1)
    await worker_http.pacing.step()
    assert world.names() == [previous]
    assert len(world.docker.runs) == 1

    wall_clock.at += timedelta(days=14)
    await worker_http.pacing.step()
    [current] = world.names()
    assert current != previous
    assert len(world.docker.runs) == 2


async def test_held_and_other_organization_snapshots_do_not_delay_a_first_backup(
    world: World,
    worker_http: WorkerHTTP,
    monkeypatch: pytest.MonkeyPatch,
    wall_clock: SnapshotClock,
    tiers: dict[UUID, OrganizationPlanId],
) -> None:
    tenant_id = world.agent.tenant_id
    assert tenant_id is not None
    tiers[tenant_id] = OrganizationPlanId.PRO
    recent = wall_clock.at - timedelta(minutes=1)
    held = new_snapshot_name("0" * 16, tenant_token(tenant_id), recent)
    former = new_snapshot_name("1" * 16, tenant_token(uuid4()), recent)
    world.store.objects |= {
        held.key(world.agent.id): b"held",
        held.held_key(world.agent.id): b"",
        former.key(world.agent.id): b"old",
    }

    start_scan(world, worker_http, monkeypatch)
    await worker_http.pacing.paused()
    [(key, _, _)] = world.store.writes
    assert set(world.store.objects) == {key, held.key(world.agent.id), held.held_key(world.agent.id)}
