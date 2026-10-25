from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest

from app.dao.schedule_dao import agent_schedule_dao
from schedule_occurrence_persistence_fakes import SLOT_COLUMNS
from test_schedule_dao import NEXT, NOW, _next_run, _Pool, _schedule, pool

__all__ = ["pool"]


async def test_edit_uses_current_pending_snapshot_even_with_stale_record(pool: _Pool) -> None:
    row = _schedule()
    pool.rows[row.id] = row
    (claim,) = await agent_schedule_dao.claim_due(NOW, _next_run)
    assert claim.lease_token is not None
    assert await agent_schedule_dao.start_occurrence(row.id, claim.lease_token, NOW)
    patch = {
        "name": "Changed",
        "instruction": "Future instruction",
        "cron_expr": "0 9 * * *",
        "timezone": "Asia/Ho_Chi_Minh",
        "next_run_at": NEXT + timedelta(hours=1),
    }
    updated = await agent_schedule_dao.update(db_obj=row, obj_in=patch)
    assert updated == replace(
        claim,
        name="Changed",
        instruction="Future instruction",
        cron_expr="0 9 * * *",
        timezone="Asia/Ho_Chi_Minh",
        next_run_at=NEXT + timedelta(hours=1),
        pending_started_at=NOW,
        last_run_at=NOW,
        run_count=row.run_count + 1,
    )
    assert patch["instruction"] == "Future instruction"


async def test_explicit_disable_discards_pending_slot_and_revokes_owner(pool: _Pool) -> None:
    row = _schedule()
    pool.rows[row.id] = row
    (claim,) = await agent_schedule_dao.claim_due(NOW, _next_run)
    assert claim.lease_token is not None
    assert await agent_schedule_dao.start_occurrence(row.id, claim.lease_token, NOW)
    patch = {"is_enabled": False, "next_run_at": NEXT}
    updated = await agent_schedule_dao.update(db_obj=row, obj_in=patch)
    assert updated == replace(row, is_enabled=False, next_run_at=None, last_run_at=NOW, run_count=row.run_count + 1)
    assert patch == {"is_enabled": False, "next_run_at": NEXT}
    assert not await agent_schedule_dao.renew_occurrence(row.id, claim.lease_token, NOW)
    enabled = await agent_schedule_dao.update(db_obj=updated, obj_in={"is_enabled": True, "next_run_at": NEXT})
    assert enabled == replace(updated, is_enabled=True, next_run_at=NEXT)
    assert not await agent_schedule_dao.start_occurrence(row.id, claim.lease_token, NOW)


@pytest.mark.parametrize("scope", ["tenant", "creator", "creator_tenant"])
async def test_bulk_disable_clears_matching_occurrences_without_crossing_scope(pool: _Pool, scope: str) -> None:
    tenant, other_tenant, creator, other_creator = uuid4(), uuid4(), uuid4(), uuid4()
    rows = [_schedule() for _ in range(4)]
    ownership = [(tenant, creator), (tenant, other_creator), (other_tenant, creator), (other_tenant, other_creator)]
    for row, owners in zip(rows, ownership, strict=True):
        pool.rows[row.id] = row
        pool.agents[row.agent_id] = owners
    await agent_schedule_dao.claim_due(NOW, _next_run)
    before = dict(pool.rows)
    expected = [0, 1] if scope == "tenant" else [0, 2] if scope == "creator" else [0]
    if scope == "tenant":
        count = await agent_schedule_dao.disable_for_tenant(tenant)
    else:
        count = await agent_schedule_dao.disable_for_creator(
            creator, tenant_id=tenant if scope == "creator_tenant" else None
        )
    assert count == len(expected)
    for index, row in enumerate(rows):
        current = pool.rows[row.id]
        if index in expected:
            assert current == replace(row, is_enabled=False, next_run_at=None)
            assert all(getattr(current, column) is None for column in SLOT_COLUMNS)
        else:
            assert current == before[row.id]
