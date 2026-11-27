"""Real PostgreSQL: ``agent_triggers.next_due_at`` on every schedule write path, the due scan and the due heartbeat selection.

Opt in with BOOTSTRAP_TEST_DATABASE_URL; each test gets a unique schema that is dropped afterwards.
"""

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import pytest

from app.core.json_types import JsonObject
from app.dao.agent_dao import agent_dao
from app.dao.tenant_dao import tenant_dao
from app.dao.trigger_dao import agent_trigger_dao
from app.db.connection import DbConnection
from app.services import agent_tools
from app.services.agent_tool_exec import _agent_tool_exec_triggers as trigger_tools, trigger_helpers
from app.services.agent_tool_exec.registry import ToolArguments
from test_openclaw_update_postgres import agent, db, member, tenant
from test_trigger_claim_capacity_postgres import queue

__all__ = ["db"]

CREATED = datetime(2026, 11, 5, 0, 30, tzinfo=UTC)
NINE_UTC = datetime(2026, 11, 5, 9, tzinfo=UTC)
NINE_HCM = datetime(2026, 11, 5, 2, tzinfo=UTC)
HCM = "Asia/Ho_Chi_Minh"
DAILY: JsonObject = {"expr": "0 9 * * *"}


async def setup(db: DbConnection, *, tenant_timezone: str = "UTC") -> tuple[UUID, UUID]:
    tenant_id = await tenant(db)
    await db.execute(
        "UPDATE tenants SET timezone = %(zone)s WHERE id = %(id)s", {"zone": tenant_timezone, "id": tenant_id}
    )
    return tenant_id, await agent(db, tenant_id, await member(db, tenant_id))


async def create(
    agent_id: UUID, trigger_type: str, config: JsonObject, name: str = "t", **columns: object
) -> tuple[UUID, datetime | None]:
    record = await agent_trigger_dao.create(
        obj_in={
            "agent_id": agent_id,
            "name": name,
            "type": trigger_type,
            "config": config,
            "created_at": CREATED,
            **columns,
        }
    )
    return record.id, record.next_due_at


async def due_at(db: DbConnection, trigger_id: UUID) -> datetime | None:
    row = await db.fetchone("SELECT next_due_at FROM agent_triggers WHERE id = %(id)s", {"id": trigger_id})
    assert row is not None
    value = row["next_due_at"]
    assert value is None or isinstance(value, datetime)
    return value


async def stored(trigger_id: UUID):
    record = await agent_trigger_dao.get(trigger_id)
    assert record is not None
    return record


async def test_create_schedules_each_time_based_type_and_leaves_webhooks_unscheduled(db: DbConnection) -> None:
    _, agent_id = await setup(db)
    at = "2026-11-07T08:00:00+00:00"
    cases: dict[str, tuple[JsonObject, datetime | None]] = {
        "cron": (DAILY, NINE_UTC),
        "once": ({"at": at}, datetime.fromisoformat(at)),
        "interval": ({"minutes": 90}, CREATED + timedelta(minutes=90)),
        "poll": ({"url": "https://example.test", "interval_min": 2}, CREATED + timedelta(minutes=5)),
        "on_message": ({"from_agent_name": "x"}, CREATED),
        "webhook": ({"token": "abc"}, None),
    }
    for trigger_type, (config, expected) in cases.items():
        trigger_id, returned = await create(agent_id, trigger_type, config, name=trigger_type)
        assert returned == expected
        assert await due_at(db, trigger_id) == expected


async def test_cron_is_scheduled_in_the_configured_then_agent_then_organization_zone(db: DbConnection) -> None:
    _, agent_id = await setup(db, tenant_timezone=HCM)
    organization, _ = await create(agent_id, "cron", DAILY, name="organization")
    configured, _ = await create(agent_id, "cron", {**DAILY, "timezone": "UTC"}, name="configured")
    assert await due_at(db, organization) == NINE_HCM
    assert await due_at(db, configured) == NINE_UTC
    await db.execute("UPDATE agents SET timezone = 'America/New_York' WHERE id = %(id)s", {"id": agent_id})
    own, _ = await create(agent_id, "cron", DAILY, name="own")
    assert await due_at(db, own) == datetime(2026, 11, 5, 14, tzinfo=UTC)


async def test_update_recomputes_for_schedule_columns_and_a_disable_clears_it(db: DbConnection) -> None:
    _, agent_id = await setup(db)
    trigger_id, _ = await create(agent_id, "cron", DAILY)
    record = await stored(trigger_id)
    record = await agent_trigger_dao.update(db_obj=record, obj_in={"config": {"expr": "0 10 * * *"}})
    assert await due_at(db, trigger_id) == NINE_UTC + timedelta(hours=1)
    record = await agent_trigger_dao.update(
        db_obj=record, obj_in={"cooldown_seconds": 12 * 3600, "last_fired_at": CREATED}
    )
    assert await due_at(db, trigger_id) == CREATED + timedelta(hours=12)
    expires = CREATED + timedelta(hours=2)
    record = await agent_trigger_dao.update(db_obj=record, obj_in={"expires_at": expires})
    assert await due_at(db, trigger_id) == expires
    record = await agent_trigger_dao.update(db_obj=record, obj_in={"is_enabled": False})
    assert await due_at(db, trigger_id) is None
    _ = await agent_trigger_dao.update(db_obj=record, obj_in={"is_enabled": True, "expires_at": None})
    assert await due_at(db, trigger_id) == CREATED + timedelta(hours=12)


async def test_merge_user_config_reschedules_the_edited_trigger(db: DbConnection) -> None:
    _, agent_id = await setup(db)
    trigger_id, _ = await create(agent_id, "interval", {"minutes": 30})
    assert await agent_trigger_dao.merge_user_config(trigger_id, {"minutes": 120})
    assert await due_at(db, trigger_id) == CREATED + timedelta(minutes=120)
    cron_id, _ = await create(agent_id, "cron", DAILY, name="cron")
    assert await agent_trigger_dao.merge_user_config(cron_id, {"timezone": HCM})
    assert await due_at(db, cron_id) == NINE_HCM


async def test_poll_checkpoint_moves_the_next_poll_one_interval_on(db: DbConnection) -> None:
    _, agent_id = await setup(db)
    trigger_id, _ = await create(agent_id, "poll", {"url": "https://example.test", "interval_min": 10})
    assert await due_at(db, trigger_id) == CREATED + timedelta(minutes=10)
    claimed = CREATED + timedelta(hours=1)
    assert await agent_trigger_dao.claim_poll_checkpoint(trigger_id, seen=None, expected_seq=0, claimed_at=claimed)
    assert await due_at(db, trigger_id) == claimed + timedelta(minutes=10)


async def test_first_start_advances_the_schedule_and_a_spent_trigger_leaves_the_index(db: DbConnection) -> None:
    _, agent_id = await setup(db)
    cron_id, _ = await create(agent_id, "cron", DAILY, name="cron")
    once_id, _ = await create(agent_id, "once", {"at": NINE_UTC.isoformat()}, name="once")
    limited_id, _ = await create(agent_id, "interval", {"minutes": 60}, name="limited", max_fires=1)
    started = NINE_UTC + timedelta(seconds=5)
    executions = [await queue(db, trigger_id, agent_id, 0) for trigger_id in (cron_id, once_id, limited_id)]
    await agent_trigger_dao.record_first_starts(executions, now=started)
    assert await due_at(db, cron_id) == NINE_UTC + timedelta(days=1)
    assert await due_at(db, once_id) is None
    assert await due_at(db, limited_id) is None


async def test_skipped_occurrence_moves_the_schedule_on(db: DbConnection) -> None:
    _, agent_id = await setup(db)
    trigger_id, _ = await create(agent_id, "cron", DAILY)
    await agent_trigger_dao.mark_skipped(trigger_id, NINE_UTC + timedelta(seconds=1))
    assert await due_at(db, trigger_id) == NINE_UTC + timedelta(days=1)


async def test_bulk_disables_clear_the_schedule(db: DbConnection) -> None:
    tenant_id, agent_id = await setup(db)
    trigger_id, _ = await create(agent_id, "cron", DAILY)
    assert await agent_trigger_dao.disable_for_tenant(tenant_id) == 1
    assert await due_at(db, trigger_id) is None
    _ = await agent_trigger_dao.update(db_obj=await stored(trigger_id), obj_in={"is_enabled": True})
    assert await due_at(db, trigger_id) == NINE_UTC
    assert await agent_trigger_dao.disable_for_agent(agent_id) == 1
    assert await due_at(db, trigger_id) is None


async def test_agent_timezone_change_reschedules_its_cron_triggers(db: DbConnection) -> None:
    _, agent_id = await setup(db)
    cron_id, _ = await create(agent_id, "cron", DAILY, name="cron")
    pinned_id, _ = await create(agent_id, "cron", {**DAILY, "timezone": "UTC"}, name="pinned")
    (record,) = await agent_dao.get_many([agent_id])
    _ = await agent_dao.update(db_obj=record, obj_in={"timezone": HCM})
    assert await due_at(db, cron_id) == NINE_HCM
    assert await due_at(db, pinned_id) == NINE_UTC


async def test_organization_timezone_change_reschedules_only_the_agents_that_follow_it(db: DbConnection) -> None:
    tenant_id, following = await setup(db)
    own = await agent(db, tenant_id, await member(db, tenant_id))
    await db.execute("UPDATE agents SET timezone = 'UTC' WHERE id = %(id)s", {"id": own})
    followed_id, _ = await create(following, "cron", DAILY, name="followed")
    own_id, _ = await create(own, "cron", DAILY, name="own")
    (record,) = await tenant_dao.get_many([tenant_id])
    _ = await tenant_dao.update(db_obj=record, obj_in={"timezone": HCM})
    assert await due_at(db, followed_id) == NINE_HCM
    assert await due_at(db, own_id) == NINE_UTC


async def test_trigger_created_through_the_agent_tool_is_scheduled(
    db: DbConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, agent_id = await setup(db, tenant_timezone=HCM)

    async def focus(*_args: object, **_kwargs: object) -> str:
        return "focus"

    async def audit(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(trigger_tools, "ensure_focus_item", focus)
    monkeypatch.setattr(trigger_helpers, "_write_trigger_audit", audit)
    arguments: ToolArguments = {"name": "daily", "type": "cron", "config": {"expr": "0 9 * * *"}, "reason": "r"}
    assert "created" in await agent_tools._handle_set_trigger(agent_id, arguments)
    created = await agent_trigger_dao.get_by_agent_and_name(agent_id, "daily")
    assert created is not None
    assert created.next_due_at is not None
    assert created.next_due_at.astimezone(UTC).hour == 2
    assert await due_at(db, created.id) == created.next_due_at
    # Cancelling clears it and setting the trigger again re-enables it with a schedule.
    assert "cancelled" in await agent_tools._handle_cancel_trigger(agent_id, {"name": "daily"})
    assert await due_at(db, created.id) is None
    assert "re-enabled" in await agent_tools._handle_set_trigger(agent_id, arguments)
    assert await due_at(db, created.id) is not None
    assert "updated" in await agent_tools._handle_update_trigger(
        agent_id, {"name": "daily", "config": {"expr": "0 10 * * *"}}
    )
    assert (await stored(created.id)).next_due_at is not None
    assert await due_at(db, created.id) != created.next_due_at


async def test_due_page_returns_only_due_enabled_rows_in_order_with_the_agent_zone(db: DbConnection) -> None:
    _, agent_id = await setup(db, tenant_timezone=HCM)
    now = NINE_UTC + timedelta(hours=1)
    late, _ = await create(agent_id, "interval", {"minutes": 600}, name="late")
    early, _ = await create(agent_id, "cron", DAILY, name="early")
    mid, _ = await create(agent_id, "interval", {"minutes": 180}, name="mid")
    _ = await create(agent_id, "webhook", {}, name="hook")
    off, _ = await create(agent_id, "interval", {"minutes": 1}, name="off")
    _ = await agent_trigger_dao.update(db_obj=await stored(off), obj_in={"is_enabled": False})
    page = await agent_trigger_dao.list_due_page(now=now, after=None, limit=10)
    assert [due.trigger.id for due in page] == [early, mid]
    assert late not in [due.trigger.id for due in page]
    assert {due.agent_timezone for due in page} == {HCM}
    first = await agent_trigger_dao.list_due_page(now=now, after=None, limit=1)
    after = (first[0].due_at, first[0].trigger.id)
    rest = await agent_trigger_dao.list_due_page(now=now, after=after, limit=10)
    assert [due.trigger.id for due in first + rest] == [early, mid]


async def test_trigger_overdue_for_days_is_still_returned_once_after_downtime(db: DbConnection) -> None:
    _, agent_id = await setup(db)
    trigger_id, _ = await create(agent_id, "cron", DAILY)
    page = await agent_trigger_dao.list_due_page(now=CREATED + timedelta(days=5), after=None, limit=10)
    assert [due.trigger.id for due in page] == [trigger_id]
    await agent_trigger_dao.record_first_starts(
        [await queue(db, trigger_id, agent_id, 0)], now=CREATED + timedelta(days=5)
    )
    assert await agent_trigger_dao.list_due_page(now=CREATED + timedelta(days=5), after=None, limit=10) == []


def _index_scans(plan: object) -> list[Mapping[str, object]]:
    found: list[Mapping[str, object]] = []
    if isinstance(plan, dict):
        node = cast("Mapping[str, object]", plan)
        if node.get("Index Name") == "ix_agent_triggers_due":
            found.append(node)
        found.extend(scan for child in node.values() for scan in _index_scans(child))
    elif isinstance(plan, list):
        found.extend(scan for child in cast("list[object]", plan) for scan in _index_scans(child))
    return found


def _seq_scans(plan: object) -> list[Mapping[str, object]]:
    found: list[Mapping[str, object]] = []
    if isinstance(plan, dict):
        node = cast("Mapping[str, object]", plan)
        if node.get("Node Type") == "Seq Scan" and node.get("Relation Name") == "agent_triggers":
            found.append(node)
        found.extend(scan for child in node.values() for scan in _seq_scans(child))
    elif isinstance(plan, list):
        found.extend(scan for child in cast("list[object]", plan) for scan in _seq_scans(child))
    return found


async def test_due_query_reads_the_partial_index_not_the_idle_triggers(
    db: DbConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given many idle triggers (due far in the future) and a few due ones
    _, agent_id = await setup(db)
    await db.execute(
        "INSERT INTO agent_triggers (id, agent_id, name, type, config, reason, fire_count, cooldown_seconds, next_due_at) "
        "SELECT gen_random_uuid(), %(agent)s, 'idle-' || n, 'interval', '{\"minutes\": 60}'::jsonb, '', 0, 60, "
        "now() + interval '1 day' + n * interval '1 second' FROM generate_series(1, 6000) AS n",
        {"agent": agent_id},
    )
    await db.execute(
        "INSERT INTO agent_triggers (id, agent_id, name, type, config, reason, fire_count, cooldown_seconds, next_due_at) "
        "SELECT gen_random_uuid(), %(agent)s, 'due-' || n, 'interval', '{\"minutes\": 60}'::jsonb, '', 0, 60, "
        "now() - n * interval '1 minute' FROM generate_series(1, 3) AS n",
        {"agent": agent_id},
    )
    await db.execute("ANALYZE agent_triggers")
    executed: list[tuple[str, Mapping[str, object]]] = []
    original = DbConnection.fetchall

    async def recording(self: DbConnection, query: str, params: Mapping[str, object] | None = None):
        executed.append((query, params or {}))
        return await original(self, query, params)

    monkeypatch.setattr(DbConnection, "fetchall", recording)
    now = datetime.now(UTC)
    # When the daemon's due page is read
    page = await agent_trigger_dao.list_due_page(now=now, after=None, limit=8)
    monkeypatch.undo()
    assert len(page) == 3
    query, params = next((q, p) for q, p in executed if "next_due_at <=" in q)
    # Then the plan scans the partial index and touches about the due rows, not the 6000 idle ones
    row = await db.fetchone("EXPLAIN (ANALYZE, FORMAT JSON) " + query, params)
    assert row is not None
    plan = json.loads(json.dumps(next(iter(row.values()))))
    scans = _index_scans(plan)
    assert scans, json.dumps(plan)[:2000]
    assert all(cast("int", scan["Actual Rows"]) <= 8 for scan in scans)
    assert not _seq_scans(plan)


async def heartbeat_agent(
    db: DbConnection,
    tenant_id: UUID,
    *,
    timezone: str | None = None,
    heartbeat_active_hours: str | None = None,
    last_heartbeat_at: datetime | None = None,
    expires_at: datetime | None = None,
    status: str = "running",
    heartbeat_enabled: bool = True,
    is_expired: bool = False,
) -> UUID:
    agent_id = await agent(db, tenant_id, await member(db, tenant_id))
    await db.execute(
        "UPDATE agents SET timezone = %(timezone)s, "
        "heartbeat_active_hours = COALESCE(%(hours)s, heartbeat_active_hours), last_heartbeat_at = %(last)s, "
        "expires_at = %(expires)s, status = %(status)s, heartbeat_enabled = %(enabled)s, is_expired = %(expired)s "
        "WHERE id = %(id)s",
        {
            "id": agent_id,
            "timezone": timezone,
            "hours": heartbeat_active_hours,
            "last": last_heartbeat_at,
            "expires": expires_at,
            "status": status,
            "enabled": heartbeat_enabled,
            "expired": is_expired,
        },
    )
    return agent_id


async def test_heartbeat_candidates_are_the_agents_whose_heartbeat_is_due(db: DbConnection) -> None:
    tenant_id = await tenant(db)
    inactive_tenant = await tenant(db, active=False)
    now = datetime(2026, 11, 5, 3, 0, tzinfo=UTC)
    hour_ago = now - timedelta(hours=1)
    long_ago = now - timedelta(days=1)
    due = {
        "never": await heartbeat_agent(db, tenant_id, heartbeat_active_hours="00:00-23:59"),
        "elapsed": await heartbeat_agent(
            db, tenant_id, last_heartbeat_at=long_ago, heartbeat_active_hours="00:00-23:59"
        ),
        "own zone": await heartbeat_agent(db, tenant_id, timezone=HCM),
        "overnight": await heartbeat_agent(db, tenant_id, heartbeat_active_hours="22:00-06:00"),
        "malformed hours": await heartbeat_agent(db, tenant_id, heartbeat_active_hours="whenever"),
        "unknown zone": await heartbeat_agent(
            db, tenant_id, timezone="Nowhere/Zone", heartbeat_active_hours="00:00-23:59"
        ),
        "expiry to persist": await heartbeat_agent(db, tenant_id, expires_at=hour_ago),
    }
    skipped = [
        await heartbeat_agent(db, tenant_id, last_heartbeat_at=hour_ago, heartbeat_active_hours="00:00-23:59"),
        await heartbeat_agent(db, tenant_id),
        await heartbeat_agent(db, tenant_id, heartbeat_active_hours="00:00-23:59", status="stopped"),
        await heartbeat_agent(db, tenant_id, heartbeat_active_hours="00:00-23:59", heartbeat_enabled=False),
        await heartbeat_agent(db, tenant_id, heartbeat_active_hours="00:00-23:59", is_expired=True),
        await heartbeat_agent(db, inactive_tenant, heartbeat_active_hours="00:00-23:59"),
        await heartbeat_agent(db, tenant_id, timezone="America/New_York"),
    ]
    found = await agent_dao.list_heartbeat_candidates(now, 100)
    assert {agent.id for agent in found} == set(due.values())
    assert not {agent.id for agent in found} & set(skipped)


async def test_heartbeat_candidates_are_bounded_longest_overdue_first(db: DbConnection) -> None:
    tenant_id = await tenant(db)
    now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    ordered = [await heartbeat_agent(db, tenant_id, last_heartbeat_at=now - timedelta(days=days)) for days in (1, 5, 3)]
    never = await heartbeat_agent(db, tenant_id)
    found = await agent_dao.list_heartbeat_candidates(now, 3)
    assert [agent.id for agent in found] == [never, ordered[1], ordered[2]]
