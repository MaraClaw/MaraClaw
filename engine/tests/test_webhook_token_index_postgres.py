import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb

from app.core.json_types import is_json_value
from app.dao.agent_dao import agent_dao
from app.dao.trigger_dao import agent_trigger_dao
from app.db.connection import DbConnection, Params
from app.db.pool import close_pool, init_pool
from app.scripts import bootstrap_db

INDEX = "ix_agent_triggers_webhook_token"
SHARED_TOKEN = "dup_token_01"


@dataclass(frozen=True, slots=True)
class WebhookDatabase:
    db: DbConnection
    agent_id: UUID


@pytest.fixture
async def webhook_db() -> AsyncIterator[WebhookDatabase]:
    conninfo = os.environ.get("BOOTSTRAP_TEST_DATABASE_URL", "")
    if not conninfo:
        pytest.skip("BOOTSTRAP_TEST_DATABASE_URL is required for isolated PostgreSQL QA")
    schema = f"webhook_token_{uuid4().hex}"
    async with await AsyncConnection[DictRow].connect(conninfo, autocommit=True, row_factory=dict_row) as raw:
        db = DbConnection(raw)
        await db.execute(f'CREATE SCHEMA "{schema}"')
        try:
            await db.execute(f'SET search_path TO "{schema}"')
            await init_pool(conninfo=make_conninfo(conninfo, options=f"-csearch_path={schema}"), min_size=1, max_size=2)
            await bootstrap_db._apply_sql_script(bootstrap_db.SCHEMA_BASELINE)
            tenant_id, owner_id, agent_id = uuid4(), uuid4(), uuid4()
            await db.execute(
                "INSERT INTO tenants (id, name, slug) VALUES (%(id)s, 'Webhook QA', 'webhook-qa')", {"id": tenant_id}
            )
            await db.execute(
                "INSERT INTO users (id, tenant_id, display_name) VALUES (%(id)s, %(tenant)s, 'Webhook QA')",
                {"id": owner_id, "tenant": tenant_id},
            )
            _ = await agent_dao.create(
                obj_in={"id": agent_id, "name": "Webhook QA", "creator_id": owner_id, "tenant_id": tenant_id}
            )
            yield WebhookDatabase(db, agent_id)
        finally:
            await close_pool()
            await db.execute(f'DROP SCHEMA "{schema}" CASCADE')


async def add_trigger(
    webhook: WebhookDatabase, name: str, *, token: str, trigger_type: str = "webhook", enabled: bool = True
) -> None:
    await webhook.db.execute(
        "INSERT INTO agent_triggers (id, agent_id, name, type, config, reason, fire_count, cooldown_seconds, is_enabled) "
        "VALUES (%(id)s, %(agent)s, %(name)s, %(type)s, %(config)s, 'qa', 0, 60, %(enabled)s)",
        {
            "id": uuid4(),
            "agent": webhook.agent_id,
            "name": name,
            "type": trigger_type,
            "config": Jsonb({"token": token}),
            "enabled": enabled,
        },
    )


def index_names(plan: object) -> set[str]:
    """Names of every index a plan node reads, found anywhere in the plan tree."""
    found: set[str] = set()
    if isinstance(plan, list):
        for child in plan:
            found |= index_names(child)
    elif isinstance(plan, dict):
        for key, value in plan.items():
            if key == "Index Name" and isinstance(value, str):
                found.add(value)
            else:
                found |= index_names(value)
    return found


async def test_replay_over_duplicate_tokens_builds_one_non_unique_partial_index(webhook_db: WebhookDatabase) -> None:
    # Given: an existing database whose webhook triggers share a token and which has no token index yet.
    db = webhook_db.db
    await db.execute(f"DROP INDEX {INDEX}")
    await add_trigger(webhook_db, "first", token=SHARED_TOKEN)
    await add_trigger(webhook_db, "second", token=SHARED_TOKEN)

    # When: the baseline is replayed over it, and then once more.
    await bootstrap_db._apply_sql_script(bootstrap_db.SCHEMA_BASELINE)
    await bootstrap_db._apply_sql_script(bootstrap_db.SCHEMA_BASELINE)

    # Then: replay succeeded, left both rows alone, and created a single non-unique partial expression index.
    indexes = await db.fetchall(
        "SELECT i.indisunique, pg_get_expr(i.indpred, i.indrelid) AS predicate, pg_get_indexdef(i.indexrelid) AS ddl "
        "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "WHERE c.relname = %(name)s AND c.relnamespace = to_regnamespace(current_schema())",
        {"name": INDEX},
    )
    assert len(indexes) == 1
    assert indexes[0]["indisunique"] is False
    assert "config ->> 'token'::text" in str(indexes[0]["ddl"])
    predicate = str(indexes[0]["predicate"])
    assert "'webhook'" in predicate
    assert "is_enabled IS TRUE" in predicate
    shared = await db.fetchval(
        "SELECT count(*) FROM agent_triggers WHERE config->>'token' = %(t)s", {"t": SHARED_TOKEN}
    )
    assert shared == 2


async def test_lookup_keeps_its_predicate_with_duplicate_disabled_and_other_type_rows(
    webhook_db: WebhookDatabase,
) -> None:
    # Given: a shared token on enabled webhooks, plus disabled and non-webhook rows with their own tokens.
    await add_trigger(webhook_db, "first", token=SHARED_TOKEN)
    await add_trigger(webhook_db, "second", token=SHARED_TOKEN)
    await add_trigger(webhook_db, "disabled", token="disabled_tk1", enabled=False)
    await add_trigger(webhook_db, "poller", token="poll_token_1", trigger_type="poll")

    # When: the DAO resolves each token through the index.
    shared = await agent_trigger_dao.find_webhook_by_token(SHARED_TOKEN)
    disabled = await agent_trigger_dao.find_webhook_by_token("disabled_tk1")
    other_type = await agent_trigger_dao.find_webhook_by_token("poll_token_1")

    # Then: a duplicate resolves to one enabled webhook; the others stay invisible.
    assert shared is not None
    assert shared.name in {"first", "second"}
    assert disabled is None
    assert other_type is None


async def test_dao_lookup_plan_uses_the_partial_token_index(
    webhook_db: WebhookDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a few thousand triggers (mostly enabled webhooks, some disabled or of another type) with collected
    # statistics, so the planner chooses by cost and no planner setting is overridden.
    db = webhook_db.db
    await db.execute(
        "INSERT INTO agent_triggers (id, agent_id, name, type, config, reason, fire_count, cooldown_seconds, is_enabled) "
        "SELECT gen_random_uuid(), %(agent)s, 'bulk-' || i, CASE WHEN i %% 5 = 0 THEN 'interval' ELSE 'webhook' END, "
        "jsonb_build_object('token', md5(i::text)), 'qa', 0, 60, i %% 4 <> 0 FROM generate_series(1, 4000) i",
        {"agent": webhook_db.agent_id},
    )
    await db.execute("ANALYZE agent_triggers")
    token = await db.fetchval("SELECT md5('1')")  # trigger bulk-1: an enabled webhook
    assert isinstance(token, str)

    # When: the DAO runs its real lookup, and that exact statement and parameters are explained.
    captured: list[tuple[str, Params]] = []
    original = DbConnection.fetchone

    async def spy(self: DbConnection, query: str, params: Params = None) -> dict[str, object] | None:
        captured.append((query, params))
        return await original(self, query, params)

    with monkeypatch.context() as patch:
        patch.setattr(DbConnection, "fetchone", spy)
        found = await agent_trigger_dao.find_webhook_by_token(token)
    assert found is not None
    assert found.config["token"] == token
    assert len(captured) == 1
    query, params = captured[0]
    plan = await db.fetchval("EXPLAIN (FORMAT JSON) " + query, params)

    # Then: the plan reads the partial token index and does not scan the table sequentially.
    assert is_json_value(plan)
    assert index_names(plan) == {INDEX}
    assert "Seq Scan" not in str(plan)
