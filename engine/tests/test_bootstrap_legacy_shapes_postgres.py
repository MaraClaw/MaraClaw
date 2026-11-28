"""Real PostgreSQL: the real bootstrap upgrades four legacy table shapes in place.

``agent_triggers`` (``next_due_at``, backfilled by the canonical scheduler in the bootstrap transaction),
``agent_workspace_usage``, ``gateway_report_deliveries`` and ``workspace_file_revisions``, each rebuilt from a current
schema with the minimal downgrade DDL. Opt in with BOOTSTRAP_TEST_DATABASE_URL; each test's schema is dropped after.
"""

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import NamedTuple
from uuid import UUID, uuid4

import anyio
import pytest
from psycopg.errors import RaiseException

from app.core.json_types import JsonObject
from app.dao.trigger_dao import agent_trigger_dao
from app.dao.workspace_dao import workspace_file_revision_dao
from app.db.connection import DbConnection, Row
from app.db.errors import CheckViolationError, DbError
from app.scripts import bootstrap_db
from test_bootstrap_postgres import schema_db
from test_openclaw_update_postgres import agent, member, tenant

__all__ = ["schema_db"]

HCM = "Asia/Ho_Chi_Minh"
CREATED = datetime(2026, 11, 5, 0, 30, tzinfo=UTC)
NOW = datetime(2026, 11, 5, 2, 45, tzinfo=UTC)
_TABLES = ["agent_triggers", "agent_workspace_usage", "gateway_report_deliveries", "workspace_file_revisions"]
_OTHERS = _TABLES[1:]


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 5, hour, minute, tzinfo=UTC)


class _Trigger(NamedTuple):
    name: str
    kind: str
    config: JsonObject
    due: datetime | None
    zone: str = "utc"
    enabled: bool = True
    fired: datetime | None = None
    fires: int = 0
    max_fires: int | None = None
    cooldown: int = 60
    expires: datetime | None = None


# A legacy row leaves last firing, max fires, expiry and the agent's own zone NULL, and configs omit their keys, so the
# schedule comes from the stored state and the defaults of the canonical scheduler alone.
_TRIGGERS = (
    _Trigger("daily-utc", "cron", {"expr": "0 9 * * *"}, _at(9)),
    _Trigger("daily-hcm", "cron", {"expr": "0 9 * * *"}, _at(2), zone="hcm"),
    _Trigger("cron-no-expr", "cron", {}, _at(0, 31)),
    _Trigger("interval-no-minutes", "interval", {}, _at(1)),
    _Trigger("once", "once", {"at": "2026-11-07T08:00:00"}, datetime(2026, 11, 7, 8, tzinfo=UTC)),
    _Trigger("poll-floor", "poll", {"interval_min": 2}, _at(0, 35)),
    _Trigger("on-message", "on_message", {}, CREATED),
    _Trigger("webhook", "webhook", {"token": "t"}, None),
    _Trigger("spent", "interval", {"minutes": 5}, None, fires=1, max_fires=1),
    _Trigger("disabled", "interval", {"minutes": 5}, None, enabled=False),
    _Trigger("cooling", "interval", {"minutes": 10}, _at(2, 30), fired=_at(2), cooldown=3600, expires=_at(2, 30)),
)
_DUE_ORDER = ["on-message", "cron-no-expr", "poll-floor", "interval-no-minutes", "daily-hcm", "cooling"]
_TRIGGER_INSERT = (
    "INSERT INTO agent_triggers (id, agent_id, name, type, config, reason, is_enabled, last_fired_at, fire_count, "
    "max_fires, cooldown_seconds, created_at, expires_at) VALUES (%(id)s, %(agent)s, %(name)s, %(kind)s, "
    "%(config)s::jsonb, 'legacy', %(enabled)s, %(fired)s, %(fires)s, %(max_fires)s, %(cooldown)s, %(created)s, %(expires)s)"
)
_CATALOG = (
    "SELECT 'column' AS kind, table_name || '.' || column_name AS name, concat_ws(' ', data_type, "
    "character_maximum_length, is_nullable, column_default) AS definition FROM information_schema.columns "
    "WHERE table_schema = current_schema() AND table_name = ANY(%(tables)s) "
    "UNION ALL SELECT 'constraint', c.relname || '.' || k.conname, pg_get_constraintdef(k.oid) FROM pg_constraint k "
    "JOIN pg_class c ON c.oid = k.conrelid WHERE c.relnamespace = current_schema()::regnamespace "
    "AND c.relname = ANY(%(tables)s) "
    "UNION ALL SELECT 'index', tablename || '.' || indexname, indexdef FROM pg_indexes "
    "WHERE schemaname = current_schema() AND tablename = ANY(%(tables)s)"
)
_OBJECTS = (
    "SELECT oid::bigint AS oid, relname::text AS name FROM pg_class WHERE relnamespace = current_schema()::regnamespace "
    "UNION ALL SELECT oid::bigint, conname::text FROM pg_constraint "
    "WHERE connamespace = current_schema()::regnamespace ORDER BY 1"
)


async def _catalog(db: DbConnection) -> dict[tuple[object, object], object]:
    """Columns, constraints and indexes of the four tables by name and expression, never by ordinal."""
    rows = await db.fetchall(_CATALOG, {"tables": _TABLES})
    return {(row["kind"], row["name"]): row["definition"] for row in rows}


async def _versions(db: DbConnection, tables: list[str]) -> dict[str, list[Row]]:
    """Row versions and table identity: an update, a rewrite or a delete-and-insert changes them."""
    return {
        table: await db.fetchall(
            "SELECT t.ctid::text AS ctid, t.xmin::text AS xmin, c.oid::bigint AS rel, c.relfilenode::bigint AS node "  # noqa: S608
            f"FROM {table} t, pg_class c WHERE c.oid = '{table}'::regclass ORDER BY 1"
        )
        for table in tables
    }


async def _rows(db: DbConnection, tables: list[str]) -> dict[str, list[Row]]:
    return {table: await db.fetchall(f"SELECT * FROM {table} ORDER BY 1") for table in tables}  # noqa: S608


def _kept(upgraded: dict[str, list[Row]], legacy: dict[str, list[Row]]) -> dict[str, list[Row]]:
    """The upgraded rows restricted to the columns the legacy rows had."""
    return {table: [{key: row[key] for key in legacy[table][0]} for row in upgraded[table]] for table in legacy}


async def _snapshot(db: DbConnection) -> tuple[dict[tuple[object, object], object], dict[str, list[Row]], list[Row]]:
    return await _catalog(db), await _versions(db, _TABLES), await db.fetchall(_OBJECTS)


async def _agent(db: DbConnection, *, zone: str | None = None, tenant_zone: str = "UTC") -> tuple[UUID, UUID]:
    tenant_id = await tenant(db)
    await db.execute("UPDATE tenants SET timezone = %(zone)s WHERE id = %(id)s", {"zone": tenant_zone, "id": tenant_id})
    agent_id = await agent(db, tenant_id, await member(db, tenant_id))
    await db.execute("UPDATE agents SET timezone = %(zone)s WHERE id = %(id)s", {"zone": zone, "id": agent_id})
    return tenant_id, agent_id


async def _legacy_triggers(db: DbConnection) -> dict[str, UUID]:
    """Downgrade ``agent_triggers`` to its shape without ``next_due_at`` and the due index, then fill it."""
    agents = {"utc": (await _agent(db))[1], "hcm": (await _agent(db, zone="", tenant_zone=HCM))[1]}
    await db.execute("DROP INDEX ix_agent_triggers_due")
    await db.execute("ALTER TABLE agent_triggers DROP COLUMN next_due_at")
    ids = {trigger.name: uuid4() for trigger in _TRIGGERS}
    for trigger in _TRIGGERS:
        params = {**trigger._asdict(), "config": json.dumps(trigger.config), "id": ids[trigger.name]}
        await db.execute(_TRIGGER_INSERT, {**params, "agent": agents[trigger.zone], "created": CREATED})
    return ids


async def _due_times(db: DbConnection) -> dict[str, object]:
    rows = await db.fetchall("SELECT name, next_due_at FROM agent_triggers")
    return {str(row["name"]): row["next_due_at"] for row in rows}


async def test_legacy_triggers_get_the_canonical_schedule_and_the_due_scan_returns_exactly_the_due_ones(
    schema_db: DbConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a current schema rebuilt as the legacy shape: no next_due_at, no due index, rows with legacy nulls.
    await bootstrap_db.main()
    fresh = await _catalog(schema_db)
    ids = await _legacy_triggers(schema_db)
    legacy_catalog, legacy_rows = await _catalog(schema_db), await _rows(schema_db, _TABLES[:1])
    assert ("column", "agent_triggers.next_due_at") not in legacy_catalog
    assert ("index", "agent_triggers.ix_agent_triggers_due") not in legacy_catalog
    # When bootstrap upgrades it, with batches of two so the keyset walk crosses five batches.
    monkeypatch.setattr(bootstrap_db, "_TRIGGER_DUE_BATCH_SIZE", 2)
    await bootstrap_db.main()
    # Then the catalog equals a fresh schema's and the legacy columns are untouched.
    assert await _catalog(schema_db) == fresh
    assert _kept(await _rows(schema_db, _TABLES[:1]), legacy_rows) == legacy_rows
    # And each due time is the canonical one: disabled and spent rows stay unscheduled, the rest follow the scheduler.
    assert await _due_times(schema_db) == {trigger.name: trigger.due for trigger in _TRIGGERS}
    # And the due scan sees exactly the rows due at NOW, in (next_due_at, id) order, across a keyset boundary.
    _ = await bootstrap_db.init_pool()
    first = await agent_trigger_dao.list_due_page(now=NOW, after=None, limit=4)
    last = first[-1]
    second = await agent_trigger_dao.list_due_page(now=NOW, after=(last.due_at, last.trigger.id), limit=4)
    assert [due.trigger.id for due in [*first, *second]] == [ids[name] for name in _DUE_ORDER]
    # And recomputing every row from its stored state gives what the upgrade stored.
    assert await agent_trigger_dao.reschedule(list(ids.values())) == {ids[t.name]: t.due for t in _TRIGGERS}
    # And a rerun touches no row version, relation, constraint or index.
    upgraded = await _snapshot(schema_db)
    await bootstrap_db.main()
    assert await _snapshot(schema_db) == upgraded


async def test_a_failed_trigger_backfill_rolls_back_the_schema_and_every_due_time_then_a_rerun_recovers(
    schema_db: DbConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    await bootstrap_db.main()
    fresh = await _catalog(schema_db)
    _ = await _legacy_triggers(schema_db)
    for ddl in _DOWNGRADES:
        await schema_db.execute(ddl)
    legacy = await _snapshot(schema_db)
    monkeypatch.setattr(bootstrap_db, "_TRIGGER_DUE_BATCH_SIZE", 2)
    reschedule = agent_trigger_dao.reschedule
    batches: list[dict[UUID, datetime | None]] = []

    async def fail_after_the_second_batch(trigger_ids: Sequence[UUID]) -> dict[UUID, datetime | None]:
        batches.append(await reschedule(trigger_ids))
        if len(batches) == 2:
            raise RuntimeError("injected after the second batch")
        return batches[-1]

    monkeypatch.setattr(agent_trigger_dao, "reschedule", fail_after_the_second_batch)
    # When the run fails after two real batches of due times were written in its transaction.
    with pytest.raises(RuntimeError, match="injected after the second batch"):
        await bootstrap_db.main()
    assert [len(batch) for batch in batches] == [2, 2]
    assert any(due is not None for batch in batches for due in batch.values())
    # Then all four tables' schema changes and every written due time roll back.
    assert await _snapshot(schema_db) == legacy
    # And the next run starts over from the legacy shape, walks every batch again and ends on the fresh schema.
    await bootstrap_db.main()
    assert len(batches) == 2 + 5
    assert await _catalog(schema_db) == fresh
    assert await _due_times(schema_db) == {trigger.name: trigger.due for trigger in _TRIGGERS}


async def test_raw_baseline_sql_refuses_a_legacy_trigger_table_with_a_hint_and_changes_nothing(
    schema_db: DbConnection,
) -> None:
    await bootstrap_db.main()
    _ = await _legacy_triggers(schema_db)
    legacy = await _snapshot(schema_db)
    baseline = await anyio.Path(bootstrap_db.SCHEMA_BASELINE).read_text(encoding="utf-8")
    # When the baseline is applied directly, without the runner that computes the due times.
    with pytest.raises(DbError) as refused:
        await schema_db.execute(baseline)
    await schema_db.execute("ROLLBACK")  # the script's own BEGIN is still open after the error
    # Then it is refused with the supported command in its hint, and the legacy shape is exactly as it was.
    cause = refused.value.orig
    assert isinstance(cause, RaiseException)
    assert "agent_triggers" in (cause.diag.message_primary or "")
    assert "app.scripts.bootstrap_db" in (cause.diag.message_hint or "")
    assert await _snapshot(schema_db) == legacy
    # And the supported path still upgrades it, after which the same raw SQL applies cleanly.
    await bootstrap_db.main()
    await schema_db.execute(baseline)
    assert await _due_times(schema_db) == {trigger.name: trigger.due for trigger in _TRIGGERS}


_REASONS = (
    "'source_agent_unavailable', 'source_tenant_changed', 'destination_agent_unavailable', "
    "'destination_tenant_changed', 'destination_relationship_changed', 'destination_session_unavailable', "
    "'payload_rejected', 'whatsapp_window_unavailable', 'whatsapp_window_expired'"
)
_REASON_CHECK = "gateway_report_deliveries_terminal_reason_check"
_DOWNGRADES = (
    "ALTER TABLE agent_workspace_usage DROP COLUMN walk_failed_at, DROP COLUMN remeasure_requested_at",
    "ALTER TABLE agent_workspace_usage ALTER COLUMN measured_at SET NOT NULL",
    "ALTER TABLE workspace_file_revisions DROP COLUMN content_omitted",
    "ALTER TABLE gateway_report_deliveries DROP COLUMN failure_detail, DROP COLUMN created_at",
    f"ALTER TABLE gateway_report_deliveries DROP CONSTRAINT {_REASON_CHECK}",
    f"ALTER TABLE gateway_report_deliveries ADD CONSTRAINT {_REASON_CHECK} CHECK (terminal_reason IN ({_REASONS}))",
)
_CUSTOM_OID = (
    "SELECT oid::bigint FROM pg_constraint WHERE conrelid = 'gateway_report_deliveries'::regclass "
    "AND conname = 'ck_custom_attempts'"
)
_USAGE_ROW = (
    "INSERT INTO agent_workspace_usage (agent_id, dir_bytes, layer_bytes, limit_bytes, measured_at, over_since, "
    "notified_at) VALUES (%(agent)s, 10, 20, 30, %(at)s, %(at)s, %(at)s)"
)
_DELIVERY_ROWS = (  # pending, delivered and terminal, each with a legacy reason or none
    "INSERT INTO gateway_report_deliveries (id, message_id, destination_kind, destination_id, tenant_id, available_at, "
    "attempts, delivered_at, terminal_reason, terminal_at) VALUES "
    "(%(pending)s, %(message)s, 'agent', gen_random_uuid(), %(tenant)s, %(at)s, 3, NULL, NULL, NULL), "
    "(gen_random_uuid(), %(message)s, 'agent', gen_random_uuid(), %(tenant)s, %(at)s, 1, %(at)s, NULL, NULL), "
    "(gen_random_uuid(), %(message)s, 'agent', gen_random_uuid(), %(tenant)s, %(at)s, 2, NULL, 'payload_rejected', %(at)s)"
)
_REVISION = (
    "INSERT INTO workspace_file_revisions (id, agent_id, path, operation, actor_type, before_content, after_content, "
    "content_hash) VALUES (%(id)s, %(agent)s, %(path)s, 'write', 'user', %(before)s, %(after)s, 'hash')"
)
_SETTLE = (
    "UPDATE gateway_report_deliveries SET terminal_reason = %(reason)s, terminal_at = %(at)s, "
    "failure_detail = %(detail)s WHERE id = %(id)s"
)


@pytest.mark.parametrize("renamed", [False, True], ids=["original-check-name", "renamed-beside-a-custom-check"])
async def test_legacy_usage_deliveries_and_revisions_keep_their_data_and_gain_defaults_and_checks(
    schema_db: DbConnection, renamed: bool
) -> None:
    # Given a current schema rebuilt as the three legacy shapes, holding rows and a custom check on deliveries.
    await bootstrap_db.main()
    fresh = await _catalog(schema_db)
    tenant_id, agent_id = await _agent(schema_db)
    _, second_agent = await _agent(schema_db)
    message, pending, revision = uuid4(), uuid4(), uuid4()
    await schema_db.execute(
        "INSERT INTO gateway_messages (id, agent_id, content, status) VALUES (%(id)s, %(agent)s, 'Keep me', 'completed')",
        {"id": message, "agent": agent_id},
    )
    for ddl in _DOWNGRADES:
        await schema_db.execute(ddl)
    await schema_db.execute(
        "ALTER TABLE gateway_report_deliveries ADD CONSTRAINT ck_custom_attempts CHECK (attempts < 50)"
    )
    if renamed:
        await schema_db.execute(f'ALTER TABLE gateway_report_deliveries RENAME CONSTRAINT {_REASON_CHECK} TO "old"')
    await schema_db.execute(_USAGE_ROW, {"agent": agent_id, "at": CREATED})
    await schema_db.execute(
        _DELIVERY_ROWS, {"pending": pending, "message": message, "tenant": tenant_id, "at": CREATED}
    )
    await schema_db.execute(
        _REVISION, {"id": revision, "agent": agent_id, "path": "a", "before": "old", "after": "new"}
    )
    legacy_rows = await _rows(schema_db, _OTHERS)
    legacy_versions = await _versions(schema_db, _OTHERS)
    custom_oid = await schema_db.fetchval(_CUSTOM_OID)
    started = await schema_db.fetchval("SELECT clock_timestamp()")
    # When bootstrap upgrades them in place.
    await bootstrap_db.main()
    finished = await schema_db.fetchval("SELECT clock_timestamp()")
    # Then the catalog is a fresh schema's plus the untouched custom check; no legacy-predicate check remains.
    custom = ("constraint", "gateway_report_deliveries.ck_custom_attempts")
    assert await _catalog(schema_db) == {**fresh, custom: "CHECK ((attempts < 50))"}
    assert await schema_db.fetchval(_CUSTOM_OID) == custom_oid
    # And every legacy row is verbatim, in the same row version, in the same relation (no rewrite).
    upgraded_rows = await _rows(schema_db, _OTHERS)
    assert _kept(upgraded_rows, legacy_rows) == legacy_rows
    assert await _versions(schema_db, _OTHERS) == legacy_versions
    # And usage gained two NULL columns and a nullable measured_at (a failed first walk stores no measurement).
    usage = upgraded_rows["agent_workspace_usage"]
    assert [(r["walk_failed_at"], r["remeasure_requested_at"]) for r in usage] == [(None, None)]
    await schema_db.execute(
        "INSERT INTO agent_workspace_usage (agent_id, dir_bytes, layer_bytes, limit_bytes, walk_failed_at) "
        "VALUES (%(agent)s, 0, 0, 1, %(at)s)",
        {"agent": second_agent, "at": CREATED},
    )
    # And revisions default content_omitted to false for old and new rows, through the typed DAO too.
    await schema_db.execute(_REVISION, {"id": uuid4(), "agent": agent_id, "path": "b", "before": None, "after": "x"})
    omitted = await schema_db.fetchall("SELECT content_omitted FROM workspace_file_revisions")
    assert [row["content_omitted"] for row in omitted] == [False, False]
    _ = await bootstrap_db.init_pool()
    old = await workspace_file_revision_dao.get_for_agent(revision, agent_id)
    assert old is not None
    assert (old.content_omitted, old.before_content, old.after_content) == (False, "old", "new")
    # And deliveries: every pre-existing row got the same upgrade-time created_at and no failure detail.
    stamps = await schema_db.fetchone(
        "SELECT count(*) AS total, count(DISTINCT created_at) AS stamps, count(failure_detail) AS details, "
        "count(*) FILTER (WHERE created_at BETWEEN %(start)s AND %(end)s) AS inside FROM gateway_report_deliveries",
        {"start": started, "end": finished},
    )
    assert stamps == {"total": 3, "stamps": 1, "details": 0, "inside": 3}
    # And the three new terminal reasons and a 200-character detail are accepted on a pending job, then reset.
    for reason in ("provider_permanent_error", "attempts_exhausted", "age_exhausted"):
        for settled in ((reason, CREATED, "x" * 200), (None, None, None)):
            await schema_db.execute(
                _SETTLE, {"reason": settled[0], "at": settled[1], "detail": settled[2], "id": pending}
            )

    async def refused(reason: str | None, at: datetime | None, detail: str | None) -> str | None:
        with pytest.raises(CheckViolationError) as raised:
            await schema_db.execute(_SETTLE, {"reason": reason, "at": at, "detail": detail, "id": pending})
        return raised.value.constraint

    # And an unknown reason, an over-long detail and a detail on a pending job are each refused by their own check.
    detail_check = next(
        str(name).split(".")[1] for (kind, name), text in fresh.items() if "failure_detail IS NULL" in str(text)
    )
    assert await refused("unknown", CREATED, None) == _REASON_CHECK
    assert await refused("age_exhausted", CREATED, "x" * 201) == "gateway_report_deliveries_failure_detail_check"
    assert await refused(None, None, "x") == detail_check
    # And a rerun changes no row version, relation, constraint or index.
    settled_state = await _snapshot(schema_db)
    await bootstrap_db.main()
    assert await _snapshot(schema_db) == settled_state
