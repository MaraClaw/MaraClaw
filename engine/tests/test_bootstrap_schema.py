"""Contract tests for the greenfield schema baseline."""

from __future__ import annotations

import re
from pathlib import Path

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
        "ix_tools_tenant_source",
        "ix_chat_messages_conv_created",
        "ix_chat_sessions_last_message",
    ):
        assert name in sql, name
