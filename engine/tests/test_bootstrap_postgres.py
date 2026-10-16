import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row

from app.db.connection import DbConnection
from app.db.errors import CheckViolationError, UniqueViolationError
from app.db.pool import EnginePool, close_pool, get_pool, init_pool
from app.scripts import bootstrap_db


@pytest.fixture
async def schema_db(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[DbConnection]:
    conninfo = os.environ.get("BOOTSTRAP_TEST_DATABASE_URL", "")
    if not conninfo:
        pytest.skip("BOOTSTRAP_TEST_DATABASE_URL is required for isolated PostgreSQL QA")
    schema = f"bootstrap_qa_{uuid4().hex}"
    async with await AsyncConnection[DictRow].connect(conninfo, autocommit=True, row_factory=dict_row) as raw:
        db = DbConnection(raw)
        await db.execute(f'CREATE SCHEMA "{schema}"')
        try:
            await db.execute(f'SET search_path TO "{schema}"')
            isolated = make_conninfo(conninfo, options=f"-csearch_path={schema}")

            async def open_isolated_pool() -> EnginePool:
                return await init_pool(conninfo=isolated, min_size=1, max_size=1)

            monkeypatch.setattr(bootstrap_db, "init_pool", open_isolated_pool)
            yield db
        finally:
            await close_pool()
            await db.execute(f'DROP SCHEMA "{schema}" CASCADE')


async def test_fresh_init_and_rerun_preserve_current_schema(schema_db: DbConnection) -> None:
    # Given an empty, uniquely owned schema.
    # When the real bootstrap initializes it and reruns.
    await bootstrap_db.main()
    await bootstrap_db.main()
    # Then current patch-origin defaults, columns, indexes, constraints and seed survive.
    columns = await schema_db.fetchall(
        "SELECT table_name, column_name, column_default, is_generated FROM information_schema.columns "
        "WHERE table_schema = current_schema()"
    )
    defaults = {(row["table_name"], row["column_name"]): row["column_default"] for row in columns}
    expected = {
        ("agents", "agent_type"): "'openclaw'::character varying",
        ("agents", "max_llm_calls_per_day"): "1000",
        ("tools", "source"): "'builtin'::character varying",
        ("agent_tools", "source"): "'system'::character varying",
        ("chat_sessions", "source_channel"): "'web'::character varying",
        ("identities", "must_change_password"): "false",
        ("users", "is_genesis"): "false",
        ("users", "quota_message_limit"): "50",
        ("users", "quota_message_period"): "'permanent'::character varying",
        ("users", "quota_messages_used"): "0",
        ("users", "quota_max_agents"): "2",
        ("users", "quota_agent_ttl_hours"): "0",
        ("llm_models", "auth_kind"): "'api_key'::character varying",
        ("tenants", "is_system"): "false",
        ("tenants", "is_default_end_user_org"): "false",
    }
    assert {key: defaults[key] for key in expected} == expected
    required = {
        ("users", "quota_period_start"),
        ("tools", "tenant_id"),
        ("agent_tools", "installed_by_agent_id"),
        ("agents", "expires_at"),
        ("agents", "is_expired"),
        ("agents", "llm_calls_today"),
        ("agents", "llm_calls_reset_at"),
        ("agents", "last_daily_reset"),
        ("agents", "last_monthly_reset"),
        ("agents", "tokens_used_total"),
        ("agents", "api_key_hash"),
        ("agents", "openclaw_last_seen"),
        ("agents", "secondary_model_id"),
        ("tenants", "sso_enabled"),
        ("tenants", "sso_domain"),
        ("tenants", "default_fallback_model_id"),
        ("tenants", "default_secondary_model_id"),
        ("llm_models", "reasoning_effort"),
        ("llm_models", "refresh_token_encrypted"),
        ("llm_models", "token_expires_at"),
        ("llm_models", "oauth_account_id"),
        ("gateway_messages", "selected_slot"),
        ("gateway_messages", "guest_model_ref"),
        ("gateway_messages", "complexity"),
        ("gateway_messages", "routing_reason"),
        ("local_departments", "name"),
        ("local_department_memberships", "department_id"),
        ("admin_audit_logs", "changes"),
        ("tenant_email_domains", "domain"),
        ("linkup_api_keys", "key_ciphertext"),
        ("linkup_async_jobs", "upstream_job_id"),
        ("web_search_events", "export_state"),
        ("web_search_export_payloads", "raw_query"),
    }
    assert required <= defaults.keys()
    assert (
        next(
            row["is_generated"] for row in columns if (row["table_name"], row["column_name"]) == ("tenants", "name_tsv")
        )
        == "ALWAYS"
    )
    indexes = await schema_db.fetchall("SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()")
    assert {
        "ux_users_identity_single_tenant",
        "ux_users_genesis_platform_admin",
        "ux_users_genesis_org_admin",
        "ix_tenants_sso_domain",
        "ix_tenants_name_tsv",
        "ux_tenants_default_end_user_org",
        "ux_tenant_email_domains_domain",
        "ux_tenant_email_domains_default",
        "ux_llm_models_tenant_grok_subscription",
        "ux_llm_models_tenant_chatgpt_subscription",
        "ux_local_departments_tenant_name",
        "ux_agent_permissions_department",
        "ix_local_department_memberships_department",
        "ix_web_search_events_export_pending",
        "ix_web_search_events_occurred_tenant",
        "ix_web_search_events_tenant_occurred",
        "ix_web_search_events_hash_occurred",
        "ix_linkup_api_keys_position",
    } <= {row["indexname"] for row in indexes}
    constraints = await schema_db.fetchall(
        "SELECT conname FROM pg_constraint WHERE connamespace = current_schema()::regnamespace"
    )
    assert {
        "ck_department_permission_use",
        "ck_linkup_key_ring_state_singleton",
        "llm_models_tenant_id_fkey",
        "tenants_default_fallback_model_id_fkey",
        "tenants_default_secondary_model_id_fkey",
        "agents_secondary_model_id_fkey",
        "local_department_memberships_user_id_tenant_id_fkey",
        "local_department_memberships_department_id_tenant_id_fkey",
    } <= {row["conname"] for row in constraints}
    assert await schema_db.fetchall("SELECT id FROM linkup_key_ring_state") == [{"id": 1}]
    enums = await schema_db.fetchall(
        "SELECT t.typname FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid "
        "WHERE t.typnamespace = current_schema()::regnamespace AND e.enumlabel = 'google_chat'"
    )
    assert {row["typname"] for row in enums} == {"im_provider_enum", "channel_type_enum"}
    with pytest.raises(RuntimeError):
        _ = get_pool()


async def test_dirty_duplicate_memberships_are_rejected(schema_db: DbConnection) -> None:
    # Given task-owned legacy duplicate memberships with the required index absent.
    await bootstrap_db.main()
    await schema_db.execute("DROP INDEX ux_users_identity_single_tenant")
    await schema_db.execute("INSERT INTO identities (id) VALUES ('00000000-0000-0000-0000-000000000001')")
    await schema_db.execute(
        "INSERT INTO tenants (id, name, slug) VALUES ('00000000-0000-0000-0000-000000000002', 'QA', 'qa')"
    )
    await schema_db.execute(
        "INSERT INTO users (id, identity_id, tenant_id, display_name) "
        "SELECT gen_random_uuid(), '00000000-0000-0000-0000-000000000001'::uuid, "
        "'00000000-0000-0000-0000-000000000002'::uuid, 'QA' FROM generate_series(1, 2)"
    )
    # When bootstrap reruns, Then it fails instead of silently accepting dirty data.
    with pytest.raises(UniqueViolationError):
        await bootstrap_db.main()


async def test_singleton_constraint_rejects_second_row(schema_db: DbConnection) -> None:
    # Given the initialized task-owned schema.
    await bootstrap_db.main()
    # When a second singleton id is inserted, Then the database rejects it.
    with pytest.raises(CheckViolationError):
        await schema_db.execute("INSERT INTO linkup_key_ring_state (id) VALUES (2)")
