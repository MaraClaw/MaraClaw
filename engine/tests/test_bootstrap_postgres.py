import os
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.errors import QueryCanceled
from psycopg.rows import DictRow, dict_row

from app.config import get_settings
from app.core.domain.users import UserRole
from app.core.json_types import int_from_row
from app.core.row_memo import clear_row_memo
from app.core.security import check_membership_generation, create_access_token, load_user_from_access_token
from app.dao.gateway_message_dao import gateway_message_dao
from app.dao.user_dao import user_dao
from app.db.connection import DbConnection
from app.db.errors import CheckViolationError, DbError, ForeignKeyViolationError, UniqueViolationError
from app.db.pool import EnginePool, close_pool, durable_pool, get_pool, init_pool
from app.db.session import connection_ctx
from app.main import app
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
        ("tenants", "sso_enabled"),
        ("tenants", "sso_domain"),
        ("tenants", "default_fallback_model_id"),
        ("llm_models", "reasoning_effort"),
        ("llm_models", "refresh_token_encrypted"),
        ("llm_models", "token_expires_at"),
        ("llm_models", "oauth_account_id"),
        ("gateway_messages", "selected_slot"),
        ("gateway_messages", "guest_model_ref"),
        ("gateway_messages", "routing_reason"),
        ("local_departments", "name"),
        ("local_department_memberships", "department_id"),
        ("admin_audit_logs", "changes"),
        ("tenant_email_domains", "domain"),
        ("tavily_api_keys", "key_ciphertext"),
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
        "ix_tavily_api_keys_position",
    } <= {row["indexname"] for row in indexes}
    constraints = await schema_db.fetchall(
        "SELECT conname, pg_get_constraintdef(oid) AS definition FROM pg_constraint "
        "WHERE connamespace = current_schema()::regnamespace"
    )
    assert {
        "ck_department_permission_use",
        "ck_tavily_key_ring_state_singleton",
        "llm_models_tenant_id_fkey",
        "tenants_default_fallback_model_id_fkey",
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
    assert await schema_db.fetchall("SELECT id FROM tavily_key_ring_state") == [{"id": 1}]
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
        await schema_db.execute("INSERT INTO tavily_key_ring_state (id) VALUES (2)")


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


_GATEWAY_COLUMNS = (
    "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns "
    "WHERE table_schema = current_schema() AND table_name = 'gateway_messages' ORDER BY column_name"
)
_GATEWAY_REPLY_FK = (
    "SELECT conname, pg_get_constraintdef(oid) AS definition FROM pg_constraint "
    "WHERE conrelid = 'gateway_messages'::regclass AND contype = 'f' AND conkey = ARRAY["
    "(SELECT attnum FROM pg_attribute WHERE attrelid = 'gateway_messages'::regclass AND attname = 'reply_binding_id')]"
)


async def test_legacy_gateway_messages_without_reply_binding_are_upgraded_in_place(schema_db: DbConnection) -> None:
    # Given a fresh schema recorded as the contract, then reverted to the pre-reply-binding table holding a pending row.
    await bootstrap_db.main()
    fresh_columns = await schema_db.fetchall(_GATEWAY_COLUMNS)
    fresh_fk = await schema_db.fetchall(_GATEWAY_REPLY_FK)
    assert [row["definition"] for row in fresh_fk] == [
        "FOREIGN KEY (reply_binding_id) REFERENCES openclaw_reply_bindings(id) ON DELETE SET NULL"
    ]
    tenant_id, user_id, agent_id, session_id, legacy_id = uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
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
        "INSERT INTO chat_sessions (id, agent_id, user_id, title) VALUES (%(session)s, %(agent)s, %(user)s, 'Legacy')",
        {"session": session_id, "agent": agent_id, "user": user_id},
    )
    await schema_db.execute(
        "INSERT INTO gateway_messages (id, agent_id, conversation_id, content, status) "
        "VALUES (%(id)s, %(agent)s, %(conversation)s, 'Keep me', 'pending')",
        {"id": legacy_id, "agent": agent_id, "conversation": str(session_id)},
    )
    await schema_db.execute("ALTER TABLE gateway_messages DROP COLUMN reply_binding_id")
    # When bootstrap upgrades it and then reruns.
    await bootstrap_db.main()
    await bootstrap_db.main()
    # Then the row survives unbound and the column, type, nullability and foreign key match the fresh definition.
    rows = await schema_db.fetchall("SELECT id, content, status, reply_binding_id FROM gateway_messages")
    assert rows == [{"id": legacy_id, "content": "Keep me", "status": "pending", "reply_binding_id": None}]
    assert await schema_db.fetchall(_GATEWAY_COLUMNS) == fresh_columns
    assert await schema_db.fetchall(_GATEWAY_REPLY_FK) == fresh_fk
    # And the gateway DAO can scan, claim (binding the reply) and create rows again.
    _ = await bootstrap_db.init_pool()
    pending, _cursor = await gateway_message_dao.pending_dispatch_page(50)
    assert [message.id for message in pending] == [legacy_id]
    claimed = await gateway_message_dao.claim_ordinary_head(legacy_id, agent_id)
    assert claimed is not None
    binding = await schema_db.fetchall(
        "SELECT id FROM openclaw_reply_bindings WHERE session_id = %(s)s", {"s": session_id}
    )
    assert [claimed.reply_binding_id] == [row["id"] for row in binding]
    created = await gateway_message_dao.create(obj_in={"agent_id": agent_id, "content": "New"})
    assert (created.content, created.reply_binding_id) == ("New", None)


_JOURNAL_RECORDED_AT = (
    "SELECT ordinal_position, data_type, is_nullable, column_default FROM information_schema.columns "
    "WHERE table_schema = current_schema() AND table_name = 'chat_history_changes' AND column_name = 'recorded_at'"
)
_JOURNAL_INDEX = (
    "SELECT indexrelid::regclass::text AS name, indexrelid::bigint AS oid, pg_get_indexdef(indexrelid) AS definition "
    "FROM pg_index WHERE indrelid = 'chat_history_changes'::regclass AND NOT indisprimary"
)
_JOURNAL_FUNCTION = "SELECT pg_get_functiondef('refresh_chat_history_revision'::regproc) AS definition"
_JOURNAL_FILENODE = "SELECT pg_relation_filenode('chat_history_changes')::bigint AS node"
_JOURNAL_ROWS = "SELECT session_id, revision, previous_revision, message_id FROM chat_history_changes ORDER BY revision"
_JOURNAL_STAMPS = "SELECT revision, recorded_at FROM chat_history_changes ORDER BY revision"
_LEGACY_JOURNAL = (
    "CREATE TABLE chat_history_changes ("
    "session_id UUID NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE, revision UUID NOT NULL, "
    "previous_revision UUID NOT NULL, message_id UUID, PRIMARY KEY (session_id, revision))"
)
_PREBUILD_JOURNAL = (
    "ALTER TABLE chat_history_changes ADD COLUMN IF NOT EXISTS recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_chat_history_changes_recorded_at ON chat_history_changes (recorded_at)",
)


@pytest.mark.parametrize("prebuilt", [False, True], ids=["bootstrap", "operator-prebuilt"])
async def test_legacy_chat_history_journal_gains_write_time_without_rewrite(
    schema_db: DbConnection, prebuilt: bool
) -> None:
    # Given a fresh schema recorded as the contract.
    await bootstrap_db.main()
    fresh_column = await schema_db.fetchall(_JOURNAL_RECORDED_AT)
    fresh_index = await schema_db.fetchall(_JOURNAL_INDEX)
    fresh_function = await schema_db.fetchall(_JOURNAL_FUNCTION)
    assert fresh_column == [
        {"ordinal_position": 5, "data_type": "timestamp with time zone", "is_nullable": "NO", "column_default": "now()"}
    ]
    assert [(row["name"], str(row["definition"]).split(" USING ")[1]) for row in fresh_index] == [
        ("ix_chat_history_changes_recorded_at", "btree (recorded_at)")
    ]
    # And a legacy journal (no write time, no index, old trigger function) populated by real message writes.
    [current_function] = [
        statement
        for statement in bootstrap_db._statement_bodies(bootstrap_db.SCHEMA_BASELINE.read_text())
        if statement.startswith("CREATE OR REPLACE FUNCTION refresh_chat_history_revision()")
    ]
    legacy_function = current_function.replace(", recorded_at)", ")").replace(" END, clock_timestamp());", " END);")
    assert "recorded_at" not in legacy_function
    tenant_id, user_id, agent_id, session_id = uuid4(), uuid4(), uuid4(), uuid4()
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
        "INSERT INTO chat_sessions (id, agent_id, user_id, title) VALUES (%(session)s, %(agent)s, %(user)s, 'Legacy')",
        {"session": session_id, "agent": agent_id, "user": user_id},
    )
    await schema_db.execute("DROP TABLE chat_history_changes")
    await schema_db.execute(_LEGACY_JOURNAL)
    await schema_db.execute(legacy_function)
    message = {"agent": agent_id, "user": user_id, "session": str(session_id)}
    await schema_db.execute(
        "INSERT INTO chat_messages (id, agent_id, user_id, role, content, conversation_id) "
        "SELECT gen_random_uuid(), %(agent)s, %(user)s, 'user', 'legacy ' || n, %(session)s "
        "FROM generate_series(1, 200) AS n",
        message,
    )
    legacy_rows = await schema_db.fetchall(_JOURNAL_ROWS)
    assert len(legacy_rows) == 200
    prebuilt_state = None
    if prebuilt:
        # The operator sequence: a metadata-only column add, then an index built without blocking writes.
        for statement in _PREBUILD_JOURNAL:
            await schema_db.execute(statement)
        prebuilt_state = (await schema_db.fetchall(_JOURNAL_STAMPS), await schema_db.fetchall(_JOURNAL_INDEX))
    node = await schema_db.fetchval(_JOURNAL_FILENODE)
    # When bootstrap upgrades it.
    await bootstrap_db.main()
    # Then no row is lost, the heap was not rewritten, and every legacy row reads one upgrade-time stamp.
    assert await schema_db.fetchall(_JOURNAL_ROWS) == legacy_rows
    assert await schema_db.fetchval(_JOURNAL_FILENODE) == node
    stamps = await schema_db.fetchall(_JOURNAL_STAMPS)
    assert len({row["recorded_at"] for row in stamps}) == 1
    # And the column, index and trigger function match the fresh schema.
    assert await schema_db.fetchall(_JOURNAL_RECORDED_AT) == fresh_column
    index = await schema_db.fetchall(_JOURNAL_INDEX)
    assert [(row["name"], row["definition"]) for row in index] == [
        (row["name"], row["definition"]) for row in fresh_index
    ]
    assert await schema_db.fetchall(_JOURNAL_FUNCTION) == fresh_function
    if prebuilt_state is not None:
        # And over a prebuilt journal bootstrap kept the operator's column values and index untouched.
        assert (stamps, index) == prebuilt_state
    # When bootstrap reruns, Then it changes nothing.
    await bootstrap_db.main()
    assert await schema_db.fetchall(_JOURNAL_ROWS) == legacy_rows
    assert await schema_db.fetchval(_JOURNAL_FILENODE) == node
    assert await schema_db.fetchall(_JOURNAL_STAMPS) == stamps
    assert await schema_db.fetchall(_JOURNAL_INDEX) == index
    # And the trigger stamps each new change with its own write time, not its transaction's start.
    async with schema_db.raw.transaction():
        started = await schema_db.fetchval("SELECT now()")
        for content in ("first", "second"):
            await schema_db.execute(
                "INSERT INTO chat_messages (id, agent_id, user_id, role, content, conversation_id) "
                "VALUES (gen_random_uuid(), %(agent)s, %(user)s, 'user', %(content)s, %(session)s)",
                {**message, "content": content},
            )
    written = await schema_db.fetchall(
        "SELECT c.recorded_at FROM chat_history_changes c JOIN chat_messages m ON m.id = c.message_id "
        "WHERE m.content IN ('first', 'second') ORDER BY m.content"
    )
    legacy, first, second = stamps[0]["recorded_at"], written[0]["recorded_at"], written[1]["recorded_at"]
    assert isinstance(legacy, datetime)
    assert isinstance(started, datetime)
    assert isinstance(first, datetime)
    assert isinstance(second, datetime)
    assert legacy < started < first < second


_USER_ROWS = "SELECT id, tenant_id, display_name, role, is_active, updated_at FROM users ORDER BY display_name"
_USER_ROWS_WITH_GENERATION = (
    "SELECT id, tenant_id, display_name, role, is_active, updated_at, membership_generation FROM users "
    "ORDER BY display_name"
)
_USERS_COLUMNS = (
    "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns "
    "WHERE table_schema = current_schema() AND table_name = 'users' ORDER BY ordinal_position"
)


async def test_legacy_users_without_membership_generation_are_upgraded_in_place(schema_db: DbConnection) -> None:
    # Given a fresh schema recorded as the contract, with the generation as the table's last column.
    await bootstrap_db.main()
    fresh_columns = await schema_db.fetchall(_USERS_COLUMNS)
    assert fresh_columns[-1] == {
        "column_name": "membership_generation",
        "data_type": "integer",
        "is_nullable": "NO",
        "column_default": "0",
    }
    # And a legacy users table, without the column, holding active and already-inactive memberships.
    tenant_id = uuid4()
    await schema_db.execute(
        "INSERT INTO tenants (id, name, slug) VALUES (%(tenant)s, 'Legacy', 'legacy')", {"tenant": tenant_id}
    )
    ids = {name: uuid4() for name in ("active-a", "active-b", "inactive-a", "inactive-b")}
    for name, user_id in ids.items():
        await schema_db.execute(
            "INSERT INTO users (id, tenant_id, display_name, is_active) VALUES (%(id)s, %(tenant)s, %(name)s, %(active)s)",
            {"id": user_id, "tenant": tenant_id, "name": name, "active": name.startswith("active")},
        )
    legacy_rows = await schema_db.fetchall(_USER_ROWS)
    await schema_db.execute("ALTER TABLE users DROP COLUMN membership_generation")

    # When bootstrap upgrades it.
    await bootstrap_db.main()

    # Then no row is lost or touched, active memberships stay at the default and inactive ones start revoked.
    rows = await schema_db.fetchall(_USER_ROWS_WITH_GENERATION)
    assert [{key: value for key, value in row.items() if key != "membership_generation"} for row in rows] == legacy_rows
    generations = {str(row["display_name"]): int_from_row(row["membership_generation"]) for row in rows}
    assert generations["active-a"] == generations["active-b"] == 0
    assert generations["inactive-a"] > 0
    assert generations["inactive-b"] > 0
    # And the column matches the fresh definition.
    assert await schema_db.fetchall(_USERS_COLUMNS) == fresh_columns
    # And a token without a membership claim (generation 0) still works for an active row and never for an inactive one.
    _ = await bootstrap_db.init_pool()
    await check_membership_generation(ids["active-a"], 0)
    with pytest.raises(HTTPException) as revoked:
        await check_membership_generation(ids["inactive-a"], 0)
    assert revoked.value.status_code == 401

    # When later writes move generations and bootstrap reruns, Then it changes nothing.
    await schema_db.execute("UPDATE users SET membership_generation = 9 WHERE display_name = 'inactive-a'")
    await schema_db.execute("UPDATE users SET membership_generation = 3 WHERE display_name = 'active-b'")
    settled = await schema_db.fetchall(_USER_ROWS_WITH_GENERATION)
    await bootstrap_db.main()
    assert await schema_db.fetchall(_USER_ROWS_WITH_GENERATION) == settled
    assert await schema_db.fetchall(_USERS_COLUMNS) == fresh_columns


async def test_deactivation_writers_revoke_earlier_sessions_and_activation_never_does(
    schema_db: DbConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a real schema: one identity with a company membership and a tenantless one, plus a peer in the company.
    await bootstrap_db.main()
    monkeypatch.setattr(get_settings(), "USER_SESSION_CACHE_TTL_SECONDS", 0)
    clear_row_memo()
    tenant_id, identity_id = uuid4(), uuid4()
    member_id, elsewhere_id, peer_id = uuid4(), uuid4(), uuid4()
    await schema_db.execute(
        "INSERT INTO tenants (id, name, slug) VALUES (%(tenant)s, 'Revocation', 'revocation')", {"tenant": tenant_id}
    )
    await schema_db.execute("INSERT INTO identities (id) VALUES (%(id)s)", {"id": identity_id})
    for user_id, identity, tenant in (
        (member_id, identity_id, tenant_id),
        (elsewhere_id, identity_id, None),
        (peer_id, None, tenant_id),
    ):
        await schema_db.execute(
            "INSERT INTO users (id, identity_id, tenant_id, display_name) VALUES (%(id)s, %(identity)s, %(tenant)s, 'QA')",
            {"id": user_id, "identity": identity, "tenant": tenant},
        )
    _ = await bootstrap_db.init_pool()

    async def generation(user_id: UUID) -> int:
        value = await user_dao.get_membership_generation(user_id)
        assert value is not None
        return value

    async def identity_generation() -> int:
        return int_from_row(
            await schema_db.fetchval("SELECT auth_generation FROM identities WHERE id = %(id)s", {"id": identity_id})
        )

    async def outcome(token: str, *, require_active: bool = True) -> int:
        clear_row_memo()
        try:
            _ = await load_user_from_access_token(token, require_active=require_active)
        except HTTPException as error:
            return error.status_code
        return 200

    identity_before = await identity_generation()
    first = await create_access_token(str(member_id), "member")
    elsewhere = await create_access_token(str(elsewhere_id), "member")
    assert await outcome(first) == 200
    assert await outcome(elsewhere) == 200
    member = await user_dao.get(member_id)
    assert member is not None

    # When the generic writer deactivates the membership, then reactivates it.
    at_start = await generation(member_id)
    _ = await user_dao.update(db_obj=member, obj_in={"is_active": False})
    deactivated = await generation(member_id)
    assert deactivated > at_start
    assert await outcome(first) == 401
    assert await outcome(first, require_active=False) == 401
    # Then repeating either write, which is no transition, leaves the generation where it was.
    _ = await user_dao.update(db_obj=member, obj_in={"is_active": False})
    _ = await user_dao.update(db_obj=member, obj_in={"is_active": True})
    _ = await user_dao.update(db_obj=member, obj_in={"is_active": True})
    assert await generation(member_id) == deactivated
    # And the earlier session stays dead, the other membership is untouched and a new session works.
    assert await outcome(first) == 401
    assert await outcome(elsewhere) == 200
    second = await create_access_token(str(member_id), "member")
    assert await outcome(second) == 200

    # When the tenant is disabled and enabled again, every deactivated member's earlier session dies.
    assert await user_dao.deactivate_for_tenant(tenant_id) == 2
    disabled = await generation(member_id)
    assert disabled > deactivated
    assert await generation(peer_id) > 0
    assert await user_dao.reactivate_for_tenant(tenant_id) == 2
    assert await generation(member_id) == disabled
    assert await outcome(second) == 401
    assert await outcome(elsewhere) == 200
    third = await create_access_token(str(member_id), "member")
    assert await outcome(third) == 200

    # When the last-admin guard writer deactivates a member that has an active peer.
    removed = await user_dao.deactivate_unless_last_active(member_id, role=UserRole.MEMBER, tenant_id=tenant_id)
    assert removed is not None
    assert await generation(member_id) > disabled
    assert await outcome(third) == 401
    # Then deactivating the already inactive membership again changes nothing.
    settled = await generation(member_id)
    assert await user_dao.deactivate_unless_last_active(member_id, role=UserRole.MEMBER, tenant_id=tenant_id) is None
    assert await generation(member_id) == settled

    # And no path touched the identity's credential generation, or a row that does not exist.
    assert await identity_generation() == identity_before
    assert await user_dao.get_membership_generation(uuid4()) is None


_LIMIT_ENV = {
    "DATABASE_STATEMENT_TIMEOUT_MS": "200",
    "DATABASE_IDLE_IN_TRANSACTION_SESSION_TIMEOUT_MS": "5000",
    "DATABASE_DURABLE_POOL_MAX_SIZE": "3",
}
_SLOW_SECONDS = 1.0


@pytest.fixture
def db_limits(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[Mapping[str, str]], None]]:
    def apply(env: Mapping[str, str]) -> None:
        for name in _LIMIT_ENV:
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        get_settings.cache_clear()

    yield apply
    get_settings.cache_clear()


@dataclass(frozen=True, slots=True)
class _Checkout:
    pid: int
    limits: tuple[str, str]


async def _checkout(pool: EnginePool) -> _Checkout:
    async with pool.connection() as raw:
        db = DbConnection(raw)
        return _Checkout(
            int_from_row(await db.fetchval("SELECT pg_backend_pid()")),
            (
                str(await db.fetchval("SHOW statement_timeout")),
                str(await db.fetchval("SHOW idle_in_transaction_session_timeout")),
            ),
        )


@pytest.mark.parametrize(
    ("env", "limits", "durable_size"),
    [
        pytest.param({}, ("30s", "1min"), 4, id="defaults"),
        pytest.param(_LIMIT_ENV, ("200ms", "5s"), 3, id="configured"),
    ],
)
async def test_both_pools_apply_limits_on_fresh_reused_and_replaced_connections(
    schema_db: DbConnection,
    db_limits: Callable[[Mapping[str, str]], None],
    env: Mapping[str, str],
    limits: tuple[str, str],
    durable_size: int,
) -> None:
    # Given the main pool (one connection) and the durable pool opened under the environment's settings.
    db_limits(env)
    main = await bootstrap_db.init_pool()
    durable = await durable_pool()
    # Then the durable pool sizes itself from its own setting instead of copying the main pool.
    assert (main.max_size, durable.max_size) == (1, durable_size)
    for pool in (main, durable):
        # When the pool serves a fresh connection, the same one again, then a replacement after a drain.
        fresh = await _checkout(pool)
        reused = await _checkout(pool)
        await pool.drain()
        replaced = await _checkout(pool)
        # Then the server reports the configured limits on every one of them.
        assert fresh.pid == reused.pid != replaced.pid
        assert (fresh.limits, reused.limits, replaced.limits) == (limits, limits, limits)


_SLOW_SEED_TRIGGER = (
    "CREATE TABLE slow_seed_hits (hit integer NOT NULL)",
    (
        "CREATE FUNCTION slow_seed() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN PERFORM pg_sleep(1); INSERT INTO slow_seed_hits VALUES (1); RETURN NEW; END $$"
    ),
    "CREATE TRIGGER slow_seed BEFORE INSERT ON tavily_key_ring_state FOR EACH ROW EXECUTE FUNCTION slow_seed()",
)
_SEED_KEY_RING = "INSERT INTO tavily_key_ring_state (id) VALUES (1) ON CONFLICT (id) DO NOTHING"


async def test_bootstrap_completes_under_tiny_limit_while_a_baseline_statement_outlasts_it(
    schema_db: DbConnection, db_limits: Callable[[Mapping[str, str]], None]
) -> None:
    # Given a 200 ms statement limit, an initialized schema, and a trigger making the baseline's seed insert take 1 s.
    db_limits(_LIMIT_ENV)
    await bootstrap_db.main()
    for statement in _SLOW_SEED_TRIGGER:
        await schema_db.execute(statement)
    pool = await bootstrap_db.init_pool()
    with pytest.raises(QueryCanceled):
        async with pool.connection() as raw:
            await raw.execute(_SEED_KEY_RING)
    await close_pool()
    assert await schema_db.fetchall("SELECT hit FROM slow_seed_hits") == []
    # When bootstrap reruns over it.
    await bootstrap_db.main()
    # Then it completes, because the slow seed statement ran under the bootstrap override.
    assert await schema_db.fetchall("SELECT hit FROM slow_seed_hits") == [{"hit": 1}]


async def _slow_through_dao() -> None:
    async with connection_ctx() as db:
        await db.execute("SELECT pg_sleep(%s)", (_SLOW_SECONDS,))


async def _slow_through_pool() -> None:
    async with get_pool().connection() as raw:
        await raw.execute("SELECT pg_sleep(%s)", (_SLOW_SECONDS,))


@pytest.mark.parametrize("slow_route", [_slow_through_dao, _slow_through_pool], ids=["dao", "direct-checkout"])
async def test_cancelled_statement_is_a_retryable_503_through_the_real_app(
    schema_db: DbConnection,
    db_limits: Callable[[Mapping[str, str]], None],
    slow_route: Callable[[], object],
) -> None:
    # Given the real app on a pool with a 200 ms statement limit and a route sleeping 1 s.
    db_limits(_LIMIT_ENV)
    _ = await bootstrap_db.init_pool()
    routes = list(app.router.routes)
    app.add_api_route("/api/qa/slow", slow_route, methods=["GET"])
    try:
        # When the server cancels the statement.
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            cancelled = await client.get("/api/qa/slow")
            # Then the client is told to retry, and the pool keeps serving afterwards.
            health = await client.get("/api/health")
    finally:
        app.router.routes[:] = routes
    assert cancelled.status_code == 503
    assert int(cancelled.headers["Retry-After"]) > 0
    assert health.status_code == 200
    with pytest.raises(DbError) as raised:
        async with connection_ctx() as db:
            await db.execute("SELECT pg_sleep(%s)", (_SLOW_SECONDS,))
    assert isinstance(raised.value.orig, QueryCanceled)
