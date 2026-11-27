"""Per-agent workspace usage rows and the measurement policy against live PostgreSQL (``MARACLAW_TEST_POSTGRES_DSN``)."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from agent_deletion_fixtures import DeletionScenario, deletion_scenario
from app.config import get_settings
from app.dao.agent_dao import agent_dao
from app.dao.agent_workspace_usage_dao import agent_workspace_usage_dao
from app.dao.tenant_dao import tenant_dao
from app.db.errors import CheckViolationError, ForeignKeyViolationError
from app.records.agent import AgentRecord
from app.records.agent_workspace_usage import AgentWorkspaceUsageRecord
from app.records.organization_plan import OrganizationPlanId
from app.services import agent_workspace_quota as quota, agent_workspace_quota_worker as worker
from app.services.agent_workspace_quota import WorkspaceDiskQuotaExceeded
from tier_limit_fixtures import set_tenant_tier

__all__ = ["deletion_scenario"]

GIB = 1024**3
PRO_LIMIT = 3_221_225_472
UNKNOWN_AGENT = UUID(int=0)


class Workspace:
    """Stands in for the directory walk so a test can choose the measured bytes without writing gigabytes."""

    def __init__(self) -> None:
        self.dir_bytes: int = 0
        self.walks: list[Path] = []
        self.timing_out: set[str] = set()

    def walk(self, path: Path) -> int:
        self.walks.append(path)
        if path.name in self.timing_out:
            raise quota.WorkspaceMeasurementTimeoutError("deadline")
        return self.dir_bytes


@pytest.fixture
def workspace(monkeypatch: pytest.MonkeyPatch) -> Workspace:
    state = Workspace()
    monkeypatch.setattr(quota, "measure_directory_bytes", state.walk)
    return state


async def _tiered_agent(
    scenario: DeletionScenario, tier: OrganizationPlanId = OrganizationPlanId.PRO, *, is_system: bool = False
) -> AgentRecord:
    await set_tenant_tier(scenario.db, scenario.tenant_id, tier)
    agent = await agent_dao.get(await scenario.agent("owner", is_system=is_system))
    assert agent is not None
    return agent


async def _record(
    agent_id: UUID, *, dir_bytes: int, layer_bytes: int, limit_bytes: int, started: datetime | None = None
) -> AgentWorkspaceUsageRecord:
    stored = await agent_workspace_usage_dao.record_measurement(
        agent_id,
        dir_bytes=dir_bytes,
        layer_bytes=layer_bytes,
        limit_bytes=limit_bytes,
        started=datetime.now(UTC) if started is None else started,
    )
    assert stored is not None
    return stored


async def _row(scenario: DeletionScenario, agent_id: UUID) -> dict[str, object] | None:
    rows = await scenario.rows(
        "SELECT dir_bytes, layer_bytes, limit_bytes, over_since, notified_at FROM agent_workspace_usage "
        "WHERE agent_id = %(id)s",
        {"id": agent_id},
    )
    return dict(rows[0]) if rows else None


async def _alerts(scenario: DeletionScenario) -> list[dict[str, object]]:
    return [
        dict(row)
        for row in await scenario.rows("SELECT user_id, agent_id, type, ref_id, title, body FROM notifications")
    ]


async def test_over_since_marks_the_overage_window_and_clears_when_back_under(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    agent = await _tiered_agent(scenario)

    under = await _record(agent.id, dir_bytes=GIB, layer_bytes=GIB // 2, limit_bytes=PRO_LIMIT)
    assert (under.used_bytes, under.over_since, under.notified_at) == (GIB + GIB // 2, None, None)

    first = await _record(agent.id, dir_bytes=2 * GIB, layer_bytes=2 * GIB, limit_bytes=PRO_LIMIT)
    assert first.over_since is not None
    assert first.measured_at is not None

    again = await _record(agent.id, dir_bytes=4 * GIB, layer_bytes=0, limit_bytes=PRO_LIMIT)
    assert again.over_since == first.over_since
    assert again.measured_at is not None
    assert again.measured_at >= first.measured_at

    exactly = await _record(agent.id, dir_bytes=PRO_LIMIT - 1, layer_bytes=1, limit_bytes=PRO_LIMIT)
    assert exactly.over_since is None


async def test_a_measurement_that_started_earlier_cannot_overwrite_a_later_one(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    agent = await _tiered_agent(scenario)
    earlier = datetime.now(UTC)
    later = earlier + timedelta(seconds=5)
    kept = await _record(agent.id, dir_bytes=GIB, layer_bytes=0, limit_bytes=PRO_LIMIT, started=later)
    assert kept.measured_at == later

    superseded = await agent_workspace_usage_dao.record_measurement(
        agent.id, dir_bytes=4 * GIB, layer_bytes=0, limit_bytes=PRO_LIMIT, started=earlier
    )

    assert superseded is None
    row = await _row(scenario, agent.id)
    assert row is not None
    assert (row["dir_bytes"], row["over_since"], row["notified_at"]) == (GIB, None, None)
    assert await agent_workspace_usage_dao.claim_notification(agent.id) is None

    same_start = await _record(agent.id, dir_bytes=4 * GIB, layer_bytes=0, limit_bytes=PRO_LIMIT, started=later)
    assert (same_start.dir_bytes, same_start.over_since) == (4 * GIB, later)

    newer = await _record(
        agent.id, dir_bytes=GIB, layer_bytes=0, limit_bytes=PRO_LIMIT, started=later + timedelta(seconds=1)
    )
    assert (newer.dir_bytes, newer.over_since) == (GIB, None)


async def test_a_failed_walk_leaves_the_stored_measurement_alone_until_a_later_start_clears_it(
    deletion_scenario: DeletionScenario,
):
    scenario = deletion_scenario
    agent = await _tiered_agent(scenario)
    dao = agent_workspace_usage_dao
    start = datetime.now(UTC)
    measured = await _record(agent.id, dir_bytes=4 * GIB, layer_bytes=GIB, limit_bytes=PRO_LIMIT, started=start)
    claimed = await dao.claim_notification(agent.id)
    assert claimed is not None

    failed_at = start + timedelta(seconds=10)
    failure = await dao.record_walk_failure(agent.id, limit_bytes=5 * GIB, started=failed_at)

    assert failure is not None
    assert failure.walk_failed_at == failed_at
    assert (failure.dir_bytes, failure.layer_bytes, failure.limit_bytes, failure.measured_at) == (
        4 * GIB,
        GIB,
        PRO_LIMIT,
        start,
    )
    assert (failure.over_since, failure.notified_at) == (measured.over_since, claimed.notified_at)
    before = await _record(
        agent.id, dir_bytes=GIB, layer_bytes=0, limit_bytes=PRO_LIMIT, started=failed_at - timedelta(seconds=1)
    )
    assert before.walk_failed_at == failed_at
    after = await _record(
        agent.id, dir_bytes=GIB, layer_bytes=0, limit_bytes=PRO_LIMIT, started=failed_at + timedelta(seconds=1)
    )
    assert (after.walk_failed_at, after.over_since) == (None, None)


async def test_a_failed_walk_of_an_unmeasured_agent_leaves_a_placeholder_that_blocks_nobody(
    deletion_scenario: DeletionScenario,
):
    scenario = deletion_scenario
    agent = await _tiered_agent(scenario)
    failed_at = datetime.now(UTC)

    placeholder = await agent_workspace_usage_dao.record_walk_failure(
        agent.id, limit_bytes=PRO_LIMIT, started=failed_at
    )

    assert placeholder is not None
    assert (placeholder.dir_bytes, placeholder.layer_bytes, placeholder.limit_bytes) == (0, 0, PRO_LIMIT)
    assert (placeholder.measured_at, placeholder.over_since, placeholder.walk_failed_at) == (None, None, failed_at)
    await quota.ensure_workspace_within_quota(agent)
    earlier = failed_at - timedelta(seconds=5)
    stored = await _record(agent.id, dir_bytes=GIB, layer_bytes=0, limit_bytes=PRO_LIMIT, started=earlier)
    assert (stored.dir_bytes, stored.measured_at, stored.walk_failed_at) == (GIB, earlier, failed_at)


async def test_a_failed_walk_that_started_before_a_stored_measurement_or_failure_is_dropped(
    deletion_scenario: DeletionScenario,
):
    scenario = deletion_scenario
    agent = await _tiered_agent(scenario)
    dao = agent_workspace_usage_dao
    start = datetime.now(UTC)
    _ = await _record(agent.id, dir_bytes=GIB, layer_bytes=0, limit_bytes=PRO_LIMIT, started=start)

    assert await dao.record_walk_failure(agent.id, limit_bytes=PRO_LIMIT, started=start - timedelta(seconds=1)) is None
    failed_at = start + timedelta(seconds=1)
    assert await dao.record_walk_failure(agent.id, limit_bytes=PRO_LIMIT, started=failed_at) is not None
    assert await dao.record_walk_failure(agent.id, limit_bytes=PRO_LIMIT, started=failed_at) is None
    assert await dao.record_walk_failure(agent.id, limit_bytes=PRO_LIMIT, started=start) is None

    later = await dao.record_walk_failure(agent.id, limit_bytes=PRO_LIMIT, started=failed_at + timedelta(seconds=1))
    assert later is not None
    assert later.walk_failed_at == failed_at + timedelta(seconds=1)


async def test_a_remeasure_request_is_cleared_only_by_a_measurement_that_started_at_or_after_it(
    deletion_scenario: DeletionScenario,
):
    scenario = deletion_scenario
    agent = await _tiered_agent(scenario)
    dao = agent_workspace_usage_dao
    start = datetime.now(UTC)
    blocked = await _record(agent.id, dir_bytes=4 * GIB, layer_bytes=0, limit_bytes=PRO_LIMIT, started=start)
    assert await dao.request_remeasure(UNKNOWN_AGENT, requested=start) is None

    asked = await dao.request_remeasure(agent.id, requested=start + timedelta(seconds=10))

    assert asked is not None
    assert (asked.remeasure_requested_at, asked.over_since) == (start + timedelta(seconds=10), blocked.over_since)
    assert await dao.request_remeasure(agent.id, requested=start + timedelta(seconds=5)) is None
    renewed = await dao.request_remeasure(agent.id, requested=start + timedelta(seconds=20))
    assert renewed is not None
    older = await _record(
        agent.id, dir_bytes=4 * GIB, layer_bytes=0, limit_bytes=PRO_LIMIT, started=start + timedelta(seconds=15)
    )
    assert older.remeasure_requested_at == start + timedelta(seconds=20)
    answered = await _record(
        agent.id, dir_bytes=GIB, layer_bytes=0, limit_bytes=PRO_LIMIT, started=start + timedelta(seconds=20)
    )
    assert (answered.remeasure_requested_at, answered.over_since) == (None, None)


async def test_priority_pages_hold_only_the_agents_asked_for_in_id_order_and_never_a_system_agent(
    deletion_scenario: DeletionScenario,
):
    scenario = deletion_scenario
    dao = agent_workspace_usage_dao
    agents = [await scenario.agent("owner", f"Agent {number}") for number in range(4)]
    system = await scenario.agent("owner", "System", is_system=True)
    now = datetime.now(UTC)
    _ = await _record(agents[0], dir_bytes=GIB, layer_bytes=0, limit_bytes=PRO_LIMIT, started=now)
    _ = await dao.record_walk_failure(agents[1], limit_bytes=PRO_LIMIT, started=now)
    _ = await _record(agents[2], dir_bytes=GIB, layer_bytes=0, limit_bytes=PRO_LIMIT, started=now)
    _ = await dao.request_remeasure(agents[2], requested=now + timedelta(seconds=1))
    _ = await dao.record_walk_failure(system, limit_bytes=PRO_LIMIT, started=now)

    async def listed(limit: int) -> list[UUID]:
        seen: list[UUID] = []
        cursor: UUID | None = None
        while page := await agent_dao.list_workspace_quota_priority_page(cursor, limit):
            assert len(page) <= limit
            seen.extend(agent.id for agent in page)
            cursor = page[-1].id
        return seen

    assert await listed(1) == sorted([agents[1], agents[2]])
    assert await listed(5) == sorted([agents[1], agents[2]])
    _ = await _record(
        agents[1], dir_bytes=GIB, layer_bytes=0, limit_bytes=PRO_LIMIT, started=now + timedelta(seconds=2)
    )
    assert await listed(5) == [agents[2]]


async def test_measurements_that_land_in_any_order_leave_the_one_that_started_last(
    deletion_scenario: DeletionScenario,
):
    scenario = deletion_scenario
    agent = await _tiered_agent(scenario)
    base = datetime.now(UTC)
    starts = [base + timedelta(seconds=number) for number in range(8)]

    results = await asyncio.gather(
        *(
            agent_workspace_usage_dao.record_measurement(
                agent.id,
                dir_bytes=4 * GIB if number % 2 else GIB,
                layer_bytes=0,
                limit_bytes=PRO_LIMIT,
                started=starts[number],
            )
            for number in (5, 2, 7, 0, 3, 6, 1, 4)
        )
    )

    row = await _row(scenario, agent.id)
    assert row is not None
    assert (row["dir_bytes"], row["over_since"] is not None) == (4 * GIB, True)
    stored = [result for result in results if result is not None]
    assert any(result.measured_at == starts[7] for result in stored)
    assert all(result.measured_at in starts for result in stored)


async def test_concurrent_measurements_of_an_over_agent_send_one_alert_and_sample_once_per_stored_measurement(
    deletion_scenario: DeletionScenario, workspace: Workspace, monkeypatch: pytest.MonkeyPatch
):
    scenario = deletion_scenario
    agent = await _tiered_agent(scenario)
    workspace.dir_bytes = 4 * GIB
    stored: list[bool] = []
    sampled: list[int] = []
    record = agent_workspace_usage_dao.record_measurement
    sample = quota.record_workspace_disk_pressure

    async def counting(
        agent_id: UUID, *, dir_bytes: int, layer_bytes: int, limit_bytes: int, started: datetime
    ) -> AgentWorkspaceUsageRecord | None:
        row = await record(
            agent_id, dir_bytes=dir_bytes, layer_bytes=layer_bytes, limit_bytes=limit_bytes, started=started
        )
        stored.append(row is not None)
        return row

    async def sampling(
        sampled_agent: AgentRecord, tier: OrganizationPlanId, *, dir_bytes: int, layer_bytes: int, limit_bytes: int
    ) -> None:
        sampled.append(dir_bytes)
        await sample(sampled_agent, tier, dir_bytes=dir_bytes, layer_bytes=layer_bytes, limit_bytes=limit_bytes)

    monkeypatch.setattr(agent_workspace_usage_dao, "record_measurement", counting)
    monkeypatch.setattr(quota, "record_workspace_disk_pressure", sampling)

    await asyncio.gather(*(quota.measure_agent(agent) for _ in range(6)))

    assert len(await _alerts(scenario)) == 1
    assert sum(stored) >= 1
    assert len(sampled) == sum(stored)
    row = await _row(scenario, agent.id)
    assert row is not None
    assert (row["over_since"] is not None, row["notified_at"] is not None) == (True, True)


class PassDoneError(Exception):
    """Ends the worker loop after its first pass, at the interval sleep."""


async def test_a_pass_measures_the_agents_asked_for_first_once_each_and_clears_what_it_answered(
    deletion_scenario: DeletionScenario, workspace: Workspace, monkeypatch: pytest.MonkeyPatch
):
    # Given four agents of which the highest id failed its walk, the next one was handed over by a delete, and the
    # highest one's walk fails again in this pass.
    scenario = deletion_scenario
    dao = agent_workspace_usage_dao
    await set_tenant_tier(scenario.db, scenario.tenant_id, OrganizationPlanId.PRO)
    ids = sorted([await scenario.agent("owner", f"Agent {number}") for number in range(4)])
    failed, handed_over = ids[3], ids[2]
    early = datetime.now(UTC) - timedelta(seconds=60)
    _ = await dao.record_walk_failure(failed, limit_bytes=PRO_LIMIT, started=early)
    _ = await _record(handed_over, dir_bytes=4 * GIB, layer_bytes=0, limit_bytes=PRO_LIMIT, started=early)
    _ = await dao.request_remeasure(handed_over, requested=early + timedelta(seconds=1))
    workspace.dir_bytes = GIB
    workspace.timing_out.add(str(failed))
    monkeypatch.setattr(get_settings(), "AGENT_WORKSPACE_QUOTA_CONCURRENCY", 1)
    monkeypatch.setattr(worker, "PAGE_SIZE", 2)

    async def stop(_seconds: float) -> None:
        raise PassDoneError

    monkeypatch.setattr(worker, "sleep", stop)
    # When one pass runs.
    with pytest.raises(PassDoneError):
        await worker.start_agent_workspace_quota_worker()
    # Then both were measured before the other two, no agent was measured twice, and only the answered one is cleared.
    assert [path.name for path in workspace.walks] == [
        str(agent_id) for agent_id in (handed_over, failed, ids[0], ids[1])
    ]
    answered = await dao.get(handed_over)
    assert answered is not None
    assert (answered.dir_bytes, answered.over_since, answered.remeasure_requested_at) == (GIB, None, None)
    still = await dao.get(failed)
    assert still is not None
    assert still.walk_failed_at is not None
    assert still.walk_failed_at > early
    assert (still.dir_bytes, still.measured_at, still.over_since) == (0, None, None)
    for agent_id in ids[:2]:
        measured = await dao.get(agent_id)
        assert measured is not None
        assert (measured.dir_bytes, measured.walk_failed_at) == (GIB, None)


async def test_the_alert_is_claimed_once_per_overage(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    agent = await _tiered_agent(scenario)
    dao = agent_workspace_usage_dao

    _ = await _record(agent.id, dir_bytes=GIB, layer_bytes=0, limit_bytes=PRO_LIMIT)
    assert await dao.claim_notification(agent.id) is None
    assert await dao.claim_notification(UNKNOWN_AGENT) is None

    _ = await _record(agent.id, dir_bytes=4 * GIB, layer_bytes=0, limit_bytes=PRO_LIMIT)
    claimed = await dao.claim_notification(agent.id)
    assert claimed is not None
    assert claimed.notified_at is not None
    assert await dao.claim_notification(agent.id) is None

    stays = await _record(agent.id, dir_bytes=5 * GIB, layer_bytes=0, limit_bytes=PRO_LIMIT)
    assert stays.notified_at == claimed.notified_at
    assert await dao.claim_notification(agent.id) is None

    cleared = await _record(agent.id, dir_bytes=GIB, layer_bytes=0, limit_bytes=PRO_LIMIT)
    assert (cleared.over_since, cleared.notified_at) == (None, None)
    _ = await _record(agent.id, dir_bytes=4 * GIB, layer_bytes=0, limit_bytes=PRO_LIMIT)
    assert await dao.claim_notification(agent.id) is not None


async def test_concurrent_claims_have_exactly_one_winner(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    agent = await _tiered_agent(scenario)
    _ = await _record(agent.id, dir_bytes=4 * GIB, layer_bytes=0, limit_bytes=PRO_LIMIT)

    results = await asyncio.gather(*(agent_workspace_usage_dao.claim_notification(agent.id) for _ in range(16)))

    assert sum(result is not None for result in results) == 1
    row = await _row(scenario, agent.id)
    assert row is not None
    assert row["notified_at"] is not None


@pytest.mark.parametrize(
    ("known_agent", "dir_bytes", "layer_bytes", "limit_bytes", "violation"),
    [
        (False, 1, 1, 1, ForeignKeyViolationError),
        (True, -1, 1, 1, CheckViolationError),
        (True, 1, -1, 1, CheckViolationError),
        (True, 1, 1, 0, CheckViolationError),
    ],
    ids=["unknown-agent", "negative-directory", "negative-layer", "zero-limit"],
)
async def test_rows_belong_to_an_agent_and_keep_sizes_sane(
    deletion_scenario: DeletionScenario,
    known_agent: bool,
    dir_bytes: int,
    layer_bytes: int,
    limit_bytes: int,
    violation: type[Exception],
):
    scenario = deletion_scenario
    agent_id = await scenario.agent("owner") if known_agent else UNKNOWN_AGENT

    with pytest.raises(violation):
        await scenario.db.execute(
            "INSERT INTO agent_workspace_usage (agent_id, dir_bytes, layer_bytes, limit_bytes, measured_at) "
            "VALUES (%(agent)s, %(dir)s, %(layer)s, %(limit)s, now())",
            {"agent": agent_id, "dir": dir_bytes, "layer": layer_bytes, "limit": limit_bytes},
        )


async def test_deleting_an_agent_or_its_company_removes_the_rows(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    agent = await _tiered_agent(scenario)
    abroad = await scenario.agent("owner", "Abroad", tenant_id=scenario.foreign_tenant_id)
    for agent_id in (agent.id, abroad):
        _ = await _record(agent_id, dir_bytes=GIB, layer_bytes=0, limit_bytes=PRO_LIMIT)

    await agent_dao.delete_with_related(agent.id)
    await tenant_dao.delete_cascade(scenario.foreign_tenant_id)

    assert await scenario.rows("SELECT agent_id FROM agent_workspace_usage") == []


async def test_candidate_pages_cover_every_tenant_agent_once_in_id_order_and_skip_system_agents(
    deletion_scenario: DeletionScenario,
):
    scenario = deletion_scenario
    ordinary = [await scenario.agent("owner", f"Agent {number}") for number in range(3)]
    ordinary.append(await scenario.agent("owner", "Abroad", tenant_id=scenario.foreign_tenant_id))
    _ = await scenario.agent("owner", "System", is_system=True)

    seen: list[UUID] = []
    cursor: UUID | None = None
    while page := await agent_dao.list_workspace_quota_page(cursor, 2):
        assert len(page) <= 2
        seen.extend(agent.id for agent in page)
        cursor = page[-1].id

    assert seen == sorted(ordinary)


async def test_an_over_agent_is_blocked_alerted_once_and_unblocked_by_a_smaller_workspace(
    deletion_scenario: DeletionScenario, workspace: Workspace, tmp_path: Path
):
    scenario = deletion_scenario
    agent = await _tiered_agent(scenario)
    workspace.dir_bytes = 4 * GIB

    await quota.measure_agent(agent)

    assert workspace.walks == [tmp_path / str(agent.id)]
    with pytest.raises(WorkspaceDiskQuotaExceeded) as refused:
        await quota.ensure_workspace_within_quota(agent)
    assert (refused.value.used_bytes, refused.value.limit_bytes) == (4 * GIB, PRO_LIMIT)
    (alert,) = await _alerts(scenario)
    assert alert["user_id"] == scenario["owner"].id
    assert alert["agent_id"] is None
    assert alert["ref_id"] == agent.id
    assert alert["type"] == "system_alert"
    assert "Delete files to continue" in str(alert["body"])

    await quota.measure_agent(agent)
    assert len(await _alerts(scenario)) == 1

    workspace.dir_bytes = GIB
    await quota.measure_agent(agent)
    await quota.ensure_workspace_within_quota(agent)
    row = await _row(scenario, agent.id)
    assert row is not None
    assert (row["over_since"], row["notified_at"]) == (None, None)

    workspace.dir_bytes = 4 * GIB
    await quota.measure_agent(agent)
    assert len(await _alerts(scenario)) == 2


async def test_a_failed_alert_rolls_its_claim_back_so_the_next_pass_sends_it(
    deletion_scenario: DeletionScenario, workspace: Workspace, monkeypatch: pytest.MonkeyPatch
):
    scenario = deletion_scenario
    agent = await _tiered_agent(scenario)
    workspace.dir_bytes = 4 * GIB
    real_send = quota.send_notification

    async def broken(**_kwargs: object) -> None:
        raise RuntimeError("notifications unavailable")

    monkeypatch.setattr(quota, "send_notification", broken)
    with pytest.raises(RuntimeError, match="notifications unavailable"):
        await quota.measure_agent(agent)
    row = await _row(scenario, agent.id)
    assert row is not None
    assert row["over_since"] is not None
    assert row["notified_at"] is None
    assert await _alerts(scenario) == []

    monkeypatch.setattr(quota, "send_notification", real_send)
    await quota.measure_agent(agent)

    assert len(await _alerts(scenario)) == 1


async def test_a_tier_upgrade_lifts_the_block_on_the_next_measurement(
    deletion_scenario: DeletionScenario, workspace: Workspace
):
    scenario = deletion_scenario
    agent = await _tiered_agent(scenario)
    workspace.dir_bytes = 4 * GIB
    await quota.measure_agent(agent)
    with pytest.raises(WorkspaceDiskQuotaExceeded):
        await quota.ensure_workspace_within_quota(agent)

    await set_tenant_tier(scenario.db, scenario.tenant_id, OrganizationPlanId.MAX)
    await quota.measure_agent(agent)

    await quota.ensure_workspace_within_quota(agent)
    row = await _row(scenario, agent.id)
    assert row is not None
    assert (row["limit_bytes"], row["over_since"]) == (5 * GIB, None)


async def test_system_agents_lose_their_stale_row_and_are_never_walked(
    deletion_scenario: DeletionScenario, workspace: Workspace
):
    scenario = deletion_scenario
    agent = await _tiered_agent(scenario, is_system=True)
    _ = await _record(agent.id, dir_bytes=4 * GIB, layer_bytes=0, limit_bytes=PRO_LIMIT)

    await quota.measure_agent(agent)

    assert await _row(scenario, agent.id) is None
    assert workspace.walks == []
    await quota.ensure_workspace_within_quota(agent)


async def test_a_file_delete_lifts_the_block_at_once_and_only_re_measures_a_blocked_agent(
    deletion_scenario: DeletionScenario, workspace: Workspace
):
    scenario = deletion_scenario
    agent = await _tiered_agent(scenario)
    workspace.dir_bytes = GIB

    await quota.refresh_workspace_usage_after_delete(agent)
    assert workspace.walks == []
    assert await _row(scenario, agent.id) is None

    _ = await _record(agent.id, dir_bytes=4 * GIB, layer_bytes=0, limit_bytes=PRO_LIMIT)
    await quota.refresh_workspace_usage_after_delete(agent)

    assert len(workspace.walks) == 1
    row = await _row(scenario, agent.id)
    assert row is not None
    assert (row["dir_bytes"], row["over_since"]) == (GIB, None)


async def test_the_real_walk_counts_the_agent_directory(deletion_scenario: DeletionScenario, tmp_path: Path):
    scenario = deletion_scenario
    agent = await _tiered_agent(scenario)
    home = tmp_path / str(agent.id)
    (home / "workspace").mkdir(parents=True)
    (home / "workspace" / "notes.txt").write_bytes(b"x" * 1234)

    await quota.measure_agent(agent)

    row = await _row(scenario, agent.id)
    assert row is not None
    assert (row["dir_bytes"], row["layer_bytes"], row["over_since"]) == (1234, 0, None)
