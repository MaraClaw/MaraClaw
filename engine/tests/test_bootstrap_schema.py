"""Contract tests for the greenfield schema baseline."""

from __future__ import annotations

import re
from pathlib import Path

from psycopg import errors as pg_errors

from app.db.errors import DbError
from app.scripts.bootstrap_db import _IGNORABLE, _is_ignorable_error, _statement_bodies

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "scripts" / "schema_baseline.sql"


def test_baseline_defines_every_referenced_enum() -> None:
    sql = BASELINE.read_text(encoding="utf-8")
    used = set(re.findall(r"\b([a-z_]+_enum)\b", sql))
    created = set(re.findall(r"CREATE TYPE\s+([a-z_]+_enum)\s+AS ENUM", sql))
    assert used, "baseline should reference enum types"
    assert used <= created, f"enums used without CREATE TYPE: {sorted(used - created)}"


def test_llm_models_fk_is_deferred_until_tenants_exist() -> None:
    sql = BASELINE.read_text(encoding="utf-8")
    tenants_at = sql.find("CREATE TABLE IF NOT EXISTS tenants")
    llm_at = sql.find("CREATE TABLE IF NOT EXISTS llm_models")
    fk_at = sql.find("llm_models_tenant_id_fkey")
    assert 0 <= llm_at < tenants_at < fk_at


def test_users_persist_genesis_flag() -> None:
    sql = BASELINE.read_text(encoding="utf-8")
    start = sql.find("CREATE TABLE IF NOT EXISTS users")
    end = sql.find("CREATE TABLE IF NOT EXISTS", start + 10)
    block = sql[start:end]
    assert "is_genesis BOOLEAN NOT NULL DEFAULT false" in block
    alter_at = sql.find("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_genesis")
    idx_at = sql.find("ux_users_genesis_platform_admin")
    assert 0 <= alter_at < idx_at


def test_bootstrap_ignores_wrapped_duplicate_errors() -> None:
    wrapped = DbError("already exists", orig=pg_errors.DuplicateTable("already exists"))
    assert _is_ignorable_error(wrapped, _IGNORABLE)
    missing = DbError(
        'column "is_genesis" does not exist',
        orig=pg_errors.UndefinedColumn('column "is_genesis" does not exist'),
    )
    assert not _is_ignorable_error(missing, _IGNORABLE)


def test_tenant_not_null_columns_have_defaults() -> None:
    sql = BASELINE.read_text(encoding="utf-8")
    start = sql.find("CREATE TABLE IF NOT EXISTS tenants")
    end = sql.find("CREATE TABLE IF NOT EXISTS", start + 10)
    block = sql[start:end]
    for column in (
        "is_active BOOLEAN NOT NULL DEFAULT true",
        "default_message_limit INTEGER NOT NULL DEFAULT 50",
        "sso_enabled BOOLEAN NOT NULL DEFAULT false",
        "a2a_async_enabled BOOLEAN NOT NULL DEFAULT true",
    ):
        assert column in block, column


def test_hot_path_indexes_are_declared() -> None:
    sql = BASELINE.read_text(encoding="utf-8")
    for name in (
        "ix_agents_tenant_id",
        "ix_agents_creator_id",
        "ix_agents_api_key_hash",
        "ix_agents_heartbeat",
        "ix_agent_tools_agent_id",
        "ix_agent_permissions_agent_id",
        "ix_agent_permissions_user_scope",
        "ix_users_tenant_id",
        "ux_users_genesis_platform_admin",
        "ux_users_genesis_org_admin",
        "ix_tools_tenant_source",
        "ix_chat_messages_conv_created",
        "ix_chat_sessions_last_message",
    ):
        assert name in sql, name


def test_statement_bodies_keeps_semicolon_inside_comment() -> None:
    sql = (
        "-- Do not rely on Alembic; this file is the source of truth.\n"
        "CREATE TABLE IF NOT EXISTS identities (id UUID NOT NULL);\n"
    )
    bodies = _statement_bodies(sql)
    assert bodies == ["CREATE TABLE IF NOT EXISTS identities (id UUID NOT NULL)"]


def test_statement_bodies_keeps_dollar_quoted_do_blocks() -> None:
    sql = (
        "DO $$ BEGIN CREATE TYPE im_provider_enum AS ENUM ('feishu', 'wecom'); "
        "EXCEPTION WHEN duplicate_object THEN NULL; END $$;\n"
        "CREATE TABLE IF NOT EXISTS identities (id UUID NOT NULL);\n"
    )
    bodies = _statement_bodies(sql)
    assert len(bodies) == 2
    assert bodies[0].startswith("DO $$ BEGIN CREATE TYPE im_provider_enum")
    assert bodies[0].endswith("END $$")
    assert bodies[1].startswith("CREATE TABLE IF NOT EXISTS identities")


def test_statement_bodies_skips_transaction_wrappers() -> None:
    sql = "BEGIN;\nCREATE TABLE IF NOT EXISTS t (id UUID);\nCOMMIT;\n"
    assert _statement_bodies(sql) == ["CREATE TABLE IF NOT EXISTS t (id UUID)"]


def test_baseline_splits_into_complete_statements() -> None:
    sql = BASELINE.read_text(encoding="utf-8")
    bodies = _statement_bodies(sql)
    assert bodies, "baseline should produce statements"
    assert all(not body.startswith("this file") for body in bodies)
    assert all(not body.upper().startswith("EXCEPTION") for body in bodies)
    assert all(not body.upper().startswith(("BEGIN", "COMMIT", "END $$")) for body in bodies)
    do_blocks = [body for body in bodies if body.lstrip().startswith("DO $$")]
    assert do_blocks, "baseline should include DO $$ enum/constraint blocks"
    assert all(body.rstrip().endswith("$$") for body in do_blocks)
