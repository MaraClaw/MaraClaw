import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.dao.trigger_dao import agent_trigger_dao
from app.db.connection import DbConnection
from test_openclaw_update_postgres import agent, db, member, tenant

__all__ = ["db"]

NOW = datetime(2026, 11, 6, tzinfo=UTC)


async def test_racing_checkpoint_claims_have_one_winner_without_changing_baseline(db: DbConnection) -> None:
    tenant_id = await tenant(db)
    agent_id = await agent(db, tenant_id, await member(db, tenant_id))
    trigger = await agent_trigger_dao.create(
        obj_in={
            "agent_id": agent_id,
            "name": "poll",
            "type": "poll",
            "config": {"url": "https://example.test/status", "_last_value": "ready", "_poll_seq": 7},
        }
    )

    results = await asyncio.gather(
        *(
            agent_trigger_dao.claim_poll_checkpoint(trigger.id, seen=None, expected_seq=7, claimed_at=NOW)
            for _ in range(2)
        )
    )

    assert sorted(results) == [False, True]
    stored = await agent_trigger_dao.get(trigger.id)
    assert stored is not None
    assert stored.config == {
        "url": "https://example.test/status",
        "_last_value": "ready",
        "_poll_seq": 7,
        "_poll_checked_at": NOW.isoformat(),
    }
    assert stored.last_fired_at is None


@pytest.mark.parametrize("edit", ["url", "json_path", "disabled"])
async def test_edit_or_disable_fences_an_obsolete_checkpoint_claim(db: DbConnection, edit: str) -> None:
    tenant_id = await tenant(db)
    agent_id = await agent(db, tenant_id, await member(db, tenant_id))
    trigger = await agent_trigger_dao.create(
        obj_in={
            "agent_id": agent_id,
            "name": "poll",
            "type": "poll",
            "config": {
                "url": "https://example.test/status",
                "json_path": "$.status",
                "_last_value": "ready",
                "_poll_seq": 7,
                "_poll_checked_at": NOW.isoformat(),
            },
        }
    )
    if edit == "disabled":
        _ = await agent_trigger_dao.update(db_obj=trigger, obj_in={"is_enabled": False})
    else:
        assert await agent_trigger_dao.merge_user_config(
            trigger.id, {edit: "https://example.test/new" if edit == "url" else "$.new"}
        )

    claimed = await agent_trigger_dao.claim_poll_checkpoint(
        trigger.id, seen=NOW.isoformat(), expected_seq=7, claimed_at=NOW + timedelta(minutes=10)
    )

    assert claimed is False
    stored = await agent_trigger_dao.get(trigger.id)
    assert stored is not None
    if edit == "disabled":
        assert stored.config == trigger.config
    else:
        assert "_poll_checked_at" not in stored.config
        assert "_last_value" not in stored.config
        assert stored.config["_poll_seq"] == 8
