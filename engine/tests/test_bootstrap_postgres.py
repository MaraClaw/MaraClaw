import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row

from app.db.connection import DbConnection
from app.db.errors import CheckViolationError, ForeignKeyViolationError, UniqueViolationError
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
        "SELECT table_name, column_name, column_default, is_generated, is_nullable, data_type "
        "FROM information_schema.columns "
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
        ("agent_schedules", "timezone"): "'UTC'::text",
        ("tenants", "is_system"): "false",
        ("tenants", "is_default_end_user_org"): "false",
        ("local_departments", "description"): "''::text",
    }
    assert {key: defaults[key] for key in expected} == expected
    description = next(
        row for row in columns if (row["table_name"], row["column_name"]) == ("local_departments", "description")
    )
    assert (description["data_type"], description["is_nullable"]) == ("text", "NO")
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
    member_columns = {row["column_name"] for row in columns if row["table_name"] == "org_members"}
    assert member_columns == {
        "id",
        "open_id",
        "unionid",
        "external_id",
        "provider_id",
        "name",
        "name_translit_full",
        "name_translit_initial",
        "email",
        "avatar_url",
        "title",
        "phone",
        "status",
        "tenant_id",
        "user_id",
    }
    assert (
        await schema_db.fetchall(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema() AND tablename = 'org_departments'"
        )
        == []
    )
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
        "SELECT conname, pg_get_constraintdef(oid) AS definition FROM pg_constraint "
        "WHERE connamespace = current_schema()::regnamespace"
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
    definitions = {row["conname"]: row["definition"] for row in constraints}
    assert definitions["org_members_tenant_id_fkey"] == "FOREIGN KEY (tenant_id) REFERENCES tenants(id)"
    assert definitions["agent_relationships_member_id_fkey"] == "FOREIGN KEY (member_id) REFERENCES org_members(id)"
    assert (
        definitions["agent_relationships_agent_id_fkey"]
        == "FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE"
    )
    for audit_column in ("created_by_user_id", "updated_by_user_id"):
        expected_fk = f"FOREIGN KEY ({audit_column}) REFERENCES users(id)"
        assert definitions[f"agent_relationships_{audit_column}_fkey"] == expected_fk
    membership_key = await schema_db.fetchall(
        "SELECT pg_get_constraintdef(oid) AS definition FROM pg_constraint "
        "WHERE connamespace = current_schema()::regnamespace "
        "AND conrelid = 'local_department_memberships'::regclass AND contype = 'p'"
    )
    assert membership_key == [{"definition": "PRIMARY KEY (user_id, department_id)"}]
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


@pytest.mark.parametrize("candidate", ["distinct", "duplicate", "other_tenant"])
async def test_department_membership_pair_constraints(schema_db: DbConnection, candidate: str) -> None:
    # Given one existing membership in a fresh, task-owned schema.
    await bootstrap_db.main()
    tenant_id, other_tenant_id, user_id = uuid4(), uuid4(), uuid4()
    first_id, second_id, foreign_id = uuid4(), uuid4(), uuid4()
    await schema_db.execute(
        "INSERT INTO tenants (id, name, slug) VALUES (%(first)s, 'First', 'first'), (%(second)s, 'Second', 'second')",
        {"first": tenant_id, "second": other_tenant_id},
    )
    await schema_db.execute(
        "INSERT INTO users (id, tenant_id, display_name) VALUES (%(user)s, %(tenant)s, 'QA member')",
        {"user": user_id, "tenant": tenant_id},
    )
    await schema_db.execute(
        "INSERT INTO local_departments (id, tenant_id, name) VALUES "
        "(%(first)s, %(tenant)s, 'Engineering'), (%(second)s, %(tenant)s, 'Operations'), "
        "(%(foreign)s, %(other_tenant)s, 'Other company')",
        {
            "first": first_id,
            "second": second_id,
            "foreign": foreign_id,
            "tenant": tenant_id,
            "other_tenant": other_tenant_id,
        },
    )
    statement = (
        "INSERT INTO local_department_memberships (user_id, tenant_id, department_id) "
        "VALUES (%(user)s, %(tenant)s, %(department)s)"
    )
    await schema_db.execute(statement, {"user": user_id, "tenant": tenant_id, "department": first_id})
    # When another membership is submitted, Then only a distinct same-tenant pair is accepted.
    match candidate:
        case "distinct":
            await schema_db.execute(statement, {"user": user_id, "tenant": tenant_id, "department": second_id})
            rows = await schema_db.fetchall(
                "SELECT department_id FROM local_department_memberships WHERE user_id = %(user)s",
                {"user": user_id},
            )
            assert {row["department_id"] for row in rows} == {first_id, second_id}
        case "duplicate":
            with pytest.raises(UniqueViolationError):
                await schema_db.execute(statement, {"user": user_id, "tenant": tenant_id, "department": first_id})
        case "other_tenant":
            with pytest.raises(ForeignKeyViolationError):
                await schema_db.execute(statement, {"user": user_id, "tenant": tenant_id, "department": foreign_id})
        case _:
            pytest.fail(f"Unknown membership scenario: {candidate}")


async def test_legacy_tasks_without_lease_columns_are_upgraded_in_place(schema_db: DbConnection) -> None:
    # Given a pre-lease tasks table holding rows, without the lease columns, constraint or index.
    await bootstrap_db.main()
    tenant_id, user_id, agent_id, task_id = uuid4(), uuid4(), uuid4(), uuid4()
    await schema_db.execute(
        "INSERT INTO tenants (id, name, slug) VALUES (%(tenant)s, 'Legacy', 'legacy')", {"tenant": tenant_id}
    )
    await schema_db.execute(
        "INSERT INTO users (id, tenant_id, display_name) VALUES (%(user)s, %(tenant)s, 'Legacy')",
        {"user": user_id, "tenant": tenant_id},
    )
    await schema_db.execute(
        "INSERT INTO agents (id, name, role_description, creator_id, tenant_id, max_triggers, min_poll_interval_min, "
        "webhook_rate_limit, access_mode, company_access_level, heartbeat_interval_minutes) "
        "VALUES (%(agent)s, 'Legacy', 'Legacy', %(user)s, %(tenant)s, 1, 1, 1, 'company', 'use', 1)",
        {"agent": agent_id, "user": user_id, "tenant": tenant_id},
    )
    await schema_db.execute(
        "INSERT INTO tasks (id, agent_id, title, type, status, priority, assignee, created_by) "
        "VALUES (%(task)s, %(agent)s, 'Keep me', 'todo', 'doing', 'high', 'self', %(user)s)",
        {"task": task_id, "agent": agent_id, "user": user_id},
    )
    await schema_db.execute("DROP INDEX ix_tasks_lease_expires_at")
    await schema_db.execute(
        "ALTER TABLE tasks DROP CONSTRAINT tasks_check, DROP COLUMN claim_token, DROP COLUMN lease_expires_at"
    )
    # When bootstrap upgrades it and then reruns.
    await bootstrap_db.main()
    await bootstrap_db.main()
    # Then rows survive with unleased state and the lease contract matches a fresh schema.
    rows = await schema_db.fetchall("SELECT title, status, claim_token, lease_expires_at FROM tasks")
    assert rows == [{"title": "Keep me", "status": "doing", "claim_token": None, "lease_expires_at": None}]
    constraints = await schema_db.fetchall(
        "SELECT conname, pg_get_constraintdef(oid) AS definition FROM pg_constraint "
        "WHERE conrelid = 'tasks'::regclass AND contype = 'c' AND conname NOT LIKE '%%not_null'"
    )
    assert constraints == [
        {"conname": "tasks_check", "definition": "CHECK (((claim_token IS NULL) = (lease_expires_at IS NULL)))"}
    ]
    index = await schema_db.fetchall(
        "SELECT indexdef LIKE '%%WHERE (claim_token IS NOT NULL)' AS is_partial "
        "FROM pg_indexes WHERE indexname = 'ix_tasks_lease_expires_at'"
    )
    assert index == [{"is_partial": True}]
    leased = await schema_db.fetchall(
        "UPDATE tasks SET claim_token = gen_random_uuid(), lease_expires_at = now() + interval '1 minute' "
        "WHERE id = %(task)s AND claim_token IS NULL RETURNING id",
        {"task": task_id},
    )
    assert leased == [{"id": task_id}]
    with pytest.raises(CheckViolationError):
        await schema_db.execute("UPDATE tasks SET lease_expires_at = NULL WHERE id = %(task)s", {"task": task_id})


_ADMISSION_CONSTRAINTS = (
    "SELECT conname, pg_get_constraintdef(oid) AS definition FROM pg_constraint "
    "WHERE conrelid = 'organization_admission_requests'::regclass AND conname LIKE 'ck_organization_admission_%%' "
    "ORDER BY conname"
)
_ADMISSION_COLUMNS = (
    "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
    "WHERE table_schema = current_schema() AND table_name = 'organization_admission_requests' ORDER BY column_name"
)


async def test_legacy_organization_admissions_are_upgraded_in_place(schema_db: DbConnection) -> None:
    # Given a fresh schema, recorded as the contract, then reverted to the pre-named-provisioning table with rows.
    await bootstrap_db.main()
    fresh_constraints = await schema_db.fetchall(_ADMISSION_CONSTRAINTS)
    fresh_columns = await schema_db.fetchall(_ADMISSION_COLUMNS)
    assert [row["conname"] for row in fresh_constraints] == [
        "ck_organization_admission_name",
        "ck_organization_admission_provisioned",
    ]
    await schema_db.execute(
        "ALTER TABLE organization_admission_requests DROP CONSTRAINT ck_organization_admission_provisioned, "
        "DROP CONSTRAINT ck_organization_admission_name, DROP COLUMN organization_name, DROP COLUMN provisioned_at, "
        "DROP COLUMN provisioned_tenant_id, DROP COLUMN provisioned_admin_user_id"
    )
    for email, status, reviewed_at in (
        ("pending@example.com", "pending", None),
        ("approved@example.com", "approved", datetime.now(UTC)),
    ):
        await schema_db.execute(
            "INSERT INTO organization_admission_requests (id,email,tier,plan,status,reviewed_at) "
            "VALUES (gen_random_uuid(),%(email)s,'pro','{\"id\":\"pro\"}'::jsonb,%(status)s,%(reviewed_at)s)",
            {"email": email, "status": status, "reviewed_at": reviewed_at},
        )
    # When bootstrap upgrades it and then reruns.
    await bootstrap_db.main()
    await bootstrap_db.main()
    # Then rows survive as unnamed and unprovisioned, and the table matches the fresh definition.
    rows = await schema_db.fetchall(
        "SELECT email, status, organization_name, provisioned_at, provisioned_tenant_id, provisioned_admin_user_id "
        "FROM organization_admission_requests ORDER BY email"
    )
    assert rows == [
        {
            "email": email,
            "status": status,
            "organization_name": None,
            "provisioned_at": None,
            "provisioned_tenant_id": None,
            "provisioned_admin_user_id": None,
        }
        for email, status in (("approved@example.com", "approved"), ("pending@example.com", "pending"))
    ]
    assert await schema_db.fetchall(_ADMISSION_CONSTRAINTS) == fresh_constraints
    assert await schema_db.fetchall(_ADMISSION_COLUMNS) == fresh_columns
    with pytest.raises(CheckViolationError):
        await schema_db.execute(
            "UPDATE organization_admission_requests SET organization_name = 'Named', status = 'approved', "
            "reviewed_at = now() WHERE email = 'pending@example.com'"
        )
    await schema_db.execute(
        "UPDATE organization_admission_requests SET organization_name = 'Named' WHERE email = 'pending@example.com'"
    )


_APPROVAL_COLUMNS = (
    "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns "
    "WHERE table_schema = current_schema() AND table_name = 'approval_requests' ORDER BY ordinal_position"
)


async def test_legacy_approvals_without_execution_args_are_upgraded_in_place(schema_db: DbConnection) -> None:
    # Given a fresh schema recorded as the contract, then reverted to the pre-replay table holding a pending row.
    await bootstrap_db.main()
    fresh_columns = await schema_db.fetchall(_APPROVAL_COLUMNS)
    assert fresh_columns[-1]["column_name"] == "execution_args"
    tenant_id, user_id, agent_id, approval_id = uuid4(), uuid4(), uuid4(), uuid4()
    await schema_db.execute(
        "INSERT INTO tenants (id, name, slug) VALUES (%(tenant)s, 'Legacy', 'legacy')", {"tenant": tenant_id}
    )
    await schema_db.execute(
        "INSERT INTO users (id, tenant_id, display_name) VALUES (%(user)s, %(tenant)s, 'Legacy')",
        {"user": user_id, "tenant": tenant_id},
    )
    await schema_db.execute(
        "INSERT INTO agents (id, name, role_description, creator_id, tenant_id, max_triggers, min_poll_interval_min, "
        "webhook_rate_limit, access_mode, company_access_level, heartbeat_interval_minutes) "
        "VALUES (%(agent)s, 'Legacy', 'Legacy', %(user)s, %(tenant)s, 1, 1, 1, 'company', 'use', 1)",
        {"agent": agent_id, "user": user_id, "tenant": tenant_id},
    )
    await schema_db.execute(
        "INSERT INTO approval_requests (id, agent_id, action_type, details, status) "
        "VALUES (%(approval)s, %(agent)s, 'write_workspace_files', "
        '\'{"tool": "write_file", "args": "{}"}\'::json, \'pending\')',
        {"approval": approval_id, "agent": agent_id},
    )
    await schema_db.execute("ALTER TABLE approval_requests DROP COLUMN execution_args")
    # When bootstrap upgrades it and then reruns.
    await bootstrap_db.main()
    await bootstrap_db.main()
    # Then the row survives without stored arguments and the table matches the fresh definition.
    rows = await schema_db.fetchall("SELECT id, details, status, execution_args FROM approval_requests")
    assert rows == [
        {
            "id": approval_id,
            "details": {"tool": "write_file", "args": "{}"},
            "status": "pending",
            "execution_args": None,
        }
    ]
    assert await schema_db.fetchall(_APPROVAL_COLUMNS) == fresh_columns
