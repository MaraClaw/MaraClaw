"""Apply baseline schema and additive patches for greenfield deploys.

Schema source of truth: ``scripts/schema_baseline.sql`` (no Alembic).
Pure-psycopg path.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from psycopg import errors as pg_errors

from app.db.pool import close_pool, init_pool
from app.db.session import connection_ctx

_IGNORABLE = (
    pg_errors.DuplicateTable,
    pg_errors.DuplicateObject,
    pg_errors.DuplicateColumn,
    pg_errors.DuplicateSchema,
)

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_BASELINE = ROOT / "scripts" / "schema_baseline.sql"

# Best-effort additive patches for older DBs that predate the baseline file.
PATCHES = [
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS quota_message_limit INTEGER DEFAULT 50",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS quota_message_period VARCHAR(20) DEFAULT 'permanent'",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS quota_messages_used INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS quota_period_start TIMESTAMPTZ",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS quota_max_agents INTEGER DEFAULT 2",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS quota_agent_ttl_hours INTEGER DEFAULT 0",
    "ALTER TABLE agents ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ",
    "ALTER TABLE agents ADD COLUMN IF NOT EXISTS is_expired BOOLEAN DEFAULT FALSE",
    "ALTER TABLE agents ADD COLUMN IF NOT EXISTS llm_calls_today INTEGER DEFAULT 0",
    "ALTER TABLE agents ADD COLUMN IF NOT EXISTS max_llm_calls_per_day INTEGER DEFAULT 1000",
    "ALTER TABLE agents ADD COLUMN IF NOT EXISTS llm_calls_reset_at TIMESTAMPTZ",
    "ALTER TABLE tools ADD COLUMN IF NOT EXISTS source VARCHAR(20) NOT NULL DEFAULT 'builtin'",
    "ALTER TABLE tools ADD COLUMN IF NOT EXISTS tenant_id UUID",
    "ALTER TABLE agent_tools ADD COLUMN IF NOT EXISTS source VARCHAR(20) NOT NULL DEFAULT 'system'",
    "ALTER TABLE agent_tools ADD COLUMN IF NOT EXISTS installed_by_agent_id UUID",
    "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS source_channel VARCHAR(20) NOT NULL DEFAULT 'web'",
    "ALTER TABLE agents ADD COLUMN IF NOT EXISTS last_daily_reset TIMESTAMPTZ",
    "ALTER TABLE agents ADD COLUMN IF NOT EXISTS last_monthly_reset TIMESTAMPTZ",
    "ALTER TABLE agents ADD COLUMN IF NOT EXISTS tokens_used_total INTEGER DEFAULT 0",
    "ALTER TABLE agents ADD COLUMN IF NOT EXISTS agent_type VARCHAR(20) NOT NULL DEFAULT 'native'",
    "ALTER TABLE agents ADD COLUMN IF NOT EXISTS api_key_hash VARCHAR(128)",
    "ALTER TABLE agents ADD COLUMN IF NOT EXISTS openclaw_last_seen TIMESTAMPTZ",
    "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS sso_enabled BOOLEAN DEFAULT FALSE",
    "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS sso_domain VARCHAR(255)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_tenants_sso_domain ON tenants(sso_domain) WHERE sso_domain IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS ix_tools_tenant_source ON tools (tenant_id, source, enabled)",
    "CREATE INDEX IF NOT EXISTS ix_users_tenant_id ON users (tenant_id)",
    "CREATE INDEX IF NOT EXISTS ix_agents_tenant_id ON agents (tenant_id)",
    "CREATE INDEX IF NOT EXISTS ix_agents_creator_id ON agents (creator_id)",
    "CREATE INDEX IF NOT EXISTS ix_agents_api_key_hash ON agents (api_key_hash) WHERE api_key_hash IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS ix_agents_heartbeat ON agents (heartbeat_enabled, status) WHERE heartbeat_enabled IS TRUE",
    "CREATE INDEX IF NOT EXISTS ix_agent_permissions_agent_id ON agent_permissions (agent_id)",
    "CREATE INDEX IF NOT EXISTS ix_agent_permissions_user_scope ON agent_permissions (scope_type, scope_id)",
    "CREATE INDEX IF NOT EXISTS ix_agent_tools_agent_id ON agent_tools (agent_id)",
    "CREATE INDEX IF NOT EXISTS ix_agent_tools_agent_tool ON agent_tools (agent_id, tool_id)",
    "CREATE INDEX IF NOT EXISTS ix_chat_messages_conv_created ON chat_messages (conversation_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_chat_sessions_last_message ON chat_sessions (agent_id, last_message_at DESC)",
    # chat / IM channel registry expansions
    "DO $$ BEGIN ALTER TYPE channel_type_enum ADD VALUE IF NOT EXISTS 'google_chat'; EXCEPTION WHEN duplicate_object THEN NULL; END $$",
    "DO $$ BEGIN ALTER TYPE im_provider_enum ADD VALUE IF NOT EXISTS 'google_chat'; EXCEPTION WHEN duplicate_object THEN NULL; END $$",
    "ALTER TABLE identities ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT false",
]


def _statement_bodies(sql: str) -> list[str]:
    cleaned = sql.replace("BEGIN;", "").replace("COMMIT;", "")
    bodies: list[str] = []
    for stmt in cleaned.split(";"):
        body = "\n".join(line for line in stmt.splitlines() if not line.strip().startswith("--")).strip()
        if body:
            bodies.append(body)
    return bodies


async def _apply_sql_script(path: Path) -> None:
    sql = path.read_text(encoding="utf-8")  # noqa: ASYNC240 - bootstrap is a one-shot CLI, not a request path
    # Strip transaction wrappers; connection_ctx already commits.
    async with connection_ctx() as conn:
        for body in _statement_bodies(sql):
            try:
                await conn.execute(body)
            except _IGNORABLE as exc:
                print(f"[bootstrap] already present: {body[:80]!r} ({exc})", flush=True)
            except Exception:
                print(f"[bootstrap] statement failed: {body[:80]!r}", flush=True)
                raise


async def main() -> None:
    await init_pool()
    try:
        if SCHEMA_BASELINE.is_file():
            print(f"[bootstrap] Applying baseline schema from {SCHEMA_BASELINE}", flush=True)
            await _apply_sql_script(SCHEMA_BASELINE)
            print("[bootstrap] Baseline schema applied", flush=True)
        else:
            print(f"[bootstrap] WARNING: missing {SCHEMA_BASELINE}", flush=True)

        print("[bootstrap] Applying additive patches", flush=True)
        for sql in PATCHES:
            try:
                async with connection_ctx() as conn:
                    await conn.execute("SET lock_timeout = '2000ms'")
                    await conn.execute(sql)
                print(f"[bootstrap] Patch applied: {sql.strip()[:80]}", flush=True)
            except _IGNORABLE as exc:
                print(f"[bootstrap] Patch already present: {sql.strip()[:80]} ({exc})", flush=True)
        print("[bootstrap] Done", flush=True)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
