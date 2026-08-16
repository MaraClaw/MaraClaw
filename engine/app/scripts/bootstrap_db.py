"""Apply baseline schema and additive patches for greenfield deploys.

Schema source of truth: ``scripts/schema_baseline.sql`` (no Alembic).
Pure-psycopg path.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from psycopg import errors as pg_errors

from app.db.errors import DbError, UniqueViolationError
from app.db.pool import close_pool, init_pool
from app.db.session import connection_ctx

_DOLLAR_TAG = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")
_TX_WRAPPERS = frozenset({"BEGIN", "COMMIT", "BEGIN TRANSACTION", "COMMIT TRANSACTION"})

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
    """
    CREATE TABLE IF NOT EXISTS admin_audit_logs (
        id UUID NOT NULL,
        actor_id UUID,
        actor_role VARCHAR(32) NOT NULL,
        actor_email VARCHAR(255),
        action VARCHAR(100) NOT NULL,
        target_type VARCHAR(50) NOT NULL,
        target_id UUID,
        tenant_id UUID,
        changes JSON NOT NULL DEFAULT '{}',
        details JSON NOT NULL DEFAULT '{}',
        ip_address VARCHAR(50),
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        PRIMARY KEY (id),
        FOREIGN KEY(actor_id) REFERENCES users (id) ON DELETE SET NULL,
        FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE SET NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_admin_audit_logs_created_at ON admin_audit_logs (created_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_admin_audit_logs_actor_id ON admin_audit_logs (actor_id)",
    "CREATE INDEX IF NOT EXISTS ix_admin_audit_logs_tenant_id ON admin_audit_logs (tenant_id)",
    "CREATE INDEX IF NOT EXISTS ix_admin_audit_logs_target ON admin_audit_logs (target_type, target_id)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_genesis BOOLEAN NOT NULL DEFAULT false",
    """
    UPDATE users SET is_genesis = TRUE
    WHERE id = (
        SELECT id FROM users WHERE role = 'platform_admin'
        ORDER BY created_at ASC NULLS LAST LIMIT 1
    )
    AND NOT EXISTS (
        SELECT 1 FROM users WHERE is_genesis IS TRUE AND role = 'platform_admin'
    )
    """,
    """
    UPDATE users AS u SET is_genesis = TRUE
    FROM (
        SELECT DISTINCT ON (tenant_id) id
        FROM users
        WHERE role = 'org_admin' AND tenant_id IS NOT NULL
        ORDER BY tenant_id, created_at ASC NULLS LAST
    ) AS g
    WHERE u.id = g.id
    AND NOT EXISTS (
        SELECT 1 FROM users AS x
        WHERE x.tenant_id = u.tenant_id AND x.is_genesis IS TRUE AND x.role = 'org_admin'
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_users_genesis_platform_admin
    ON users (role) WHERE is_genesis IS TRUE AND role = 'platform_admin'
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_users_genesis_org_admin
    ON users (tenant_id) WHERE is_genesis IS TRUE AND role = 'org_admin'
    """,
    "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS is_system BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS is_default_end_user_org BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS default_fallback_model_id UUID",
    "ALTER TABLE llm_models ADD COLUMN IF NOT EXISTS reasoning_effort VARCHAR(16)",
    """
    DO $$ BEGIN
        ALTER TABLE tenants
            ADD CONSTRAINT tenants_default_fallback_model_id_fkey
            FOREIGN KEY (default_fallback_model_id) REFERENCES llm_models (id) ON DELETE SET NULL;
    EXCEPTION WHEN duplicate_object THEN NULL;
    END $$
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_tenants_default_end_user_org
    ON tenants (is_default_end_user_org) WHERE is_default_end_user_org IS TRUE
    """,
    """
    CREATE TABLE IF NOT EXISTS tenant_email_domains (
        id UUID NOT NULL,
        tenant_id UUID NOT NULL,
        domain VARCHAR(255) NOT NULL,
        is_default BOOLEAN NOT NULL DEFAULT false,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        PRIMARY KEY (id),
        FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE CASCADE
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_tenant_email_domains_domain ON tenant_email_domains (domain)",
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_tenant_email_domains_default
    ON tenant_email_domains (tenant_id) WHERE is_default IS TRUE
    """,
    "CREATE INDEX IF NOT EXISTS ix_tenant_email_domains_tenant_id ON tenant_email_domains (tenant_id)",
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_users_identity_single_tenant
    ON users (identity_id) WHERE tenant_id IS NOT NULL
    """,
    """
    ALTER TABLE tenants ADD COLUMN IF NOT EXISTS name_tsv tsvector
    GENERATED ALWAYS AS (
        to_tsvector('simple'::regconfig, coalesce(name, '') || ' ' || coalesce(slug, ''))
    ) STORED
    """,
    "CREATE INDEX IF NOT EXISTS ix_tenants_name_tsv ON tenants USING GIN (name_tsv)",
    """
    CREATE TABLE IF NOT EXISTS linkup_api_keys (
        id UUID NOT NULL,
        tenant_id UUID,
        label VARCHAR(200) NOT NULL,
        key_ciphertext TEXT NOT NULL,
        key_fingerprint VARCHAR(64) NOT NULL,
        position INTEGER NOT NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'active',
        exhausted_until TIMESTAMP WITH TIME ZONE,
        last_error TEXT,
        last_used_at TIMESTAMP WITH TIME ZONE,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT uq_linkup_api_keys_fingerprint UNIQUE (key_fingerprint)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_linkup_api_keys_position ON linkup_api_keys (position)",
    """
    CREATE TABLE IF NOT EXISTS linkup_key_ring_state (
        id SMALLINT NOT NULL DEFAULT 1,
        current_key_id UUID,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT ck_linkup_key_ring_state_singleton CHECK (id = 1)
    )
    """,
    "INSERT INTO linkup_key_ring_state (id) VALUES (1) ON CONFLICT (id) DO NOTHING",
    """
    CREATE TABLE IF NOT EXISTS linkup_async_jobs (
        upstream_job_id VARCHAR(200) NOT NULL,
        key_id UUID NOT NULL,
        kind VARCHAR(20) NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        PRIMARY KEY (upstream_job_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS web_search_events (
        id UUID NOT NULL,
        occurred_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        agent_id UUID,
        tenant_id UUID,
        kind VARCHAR(20) NOT NULL,
        billed BOOLEAN NOT NULL DEFAULT true,
        method VARCHAR(10) NOT NULL,
        http_status INTEGER NOT NULL,
        status_class VARCHAR(20) NOT NULL,
        latency_ms INTEGER NOT NULL,
        key_id UUID,
        query_hash VARCHAR(64) NOT NULL,
        query_normalized VARCHAR(500) NOT NULL DEFAULT '',
        query_char_len INTEGER NOT NULL DEFAULT 0,
        depth VARCHAR(20),
        output_type VARCHAR(40),
        primary_domain VARCHAR(255),
        result_count INTEGER,
        error_class VARCHAR(40),
        upstream_job_id VARCHAR(200),
        request_bytes INTEGER,
        response_bytes INTEGER,
        export_state VARCHAR(20) NOT NULL DEFAULT 'skipped',
        export_claimed_at TIMESTAMP WITH TIME ZONE,
        exported_at TIMESTAMP WITH TIME ZONE,
        schema_version INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_web_search_events_occurred_tenant ON web_search_events (occurred_at DESC, tenant_id)",
    "CREATE INDEX IF NOT EXISTS ix_web_search_events_tenant_occurred ON web_search_events (tenant_id, occurred_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_web_search_events_hash_occurred ON web_search_events (query_hash, occurred_at DESC)",
    """
    CREATE INDEX IF NOT EXISTS ix_web_search_events_export_pending
    ON web_search_events (export_state, occurred_at)
    WHERE export_state IN ('pending', 'exporting')
    """,
    """
    CREATE TABLE IF NOT EXISTS web_search_export_payloads (
        event_id UUID NOT NULL,
        raw_query TEXT NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        PRIMARY KEY (event_id)
    )
    """,
]


def _unwrap_pg_error(exc: BaseException) -> BaseException:
    """Return the underlying psycopg error when ``conn.execute`` wrapped it."""
    if isinstance(exc, DbError) and isinstance(exc.orig, BaseException):
        return exc.orig
    return exc


def _is_ignorable_error(exc: BaseException, kinds: tuple[type[BaseException], ...]) -> bool:
    return isinstance(_unwrap_pg_error(exc), kinds)


def _clean_statement(stmt: str) -> str:
    body = "\n".join(line for line in stmt.splitlines() if not line.strip().startswith("--")).strip()
    if body.upper() in _TX_WRAPPERS:
        return ""
    return body


def _statement_bodies(sql: str) -> list[str]:
    """Split SQL on top-level semicolons.

    Semicolons inside ``--`` / ``/* */`` comments, quoted strings, and
    dollar-quoted bodies (``DO $$ ... $$``) are not statement boundaries.
    Transaction wrappers are dropped because ``connection_ctx`` already commits.
    """
    bodies: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(sql)
    in_line_comment = False
    in_block_comment = False
    in_single = False
    in_double = False
    dollar_tag: str | None = None

    def flush() -> None:
        body = _clean_statement("".join(buf))
        buf.clear()
        if body:
            bodies.append(body)

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if in_line_comment:
            buf.append(ch)
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue

        if in_block_comment:
            buf.append(ch)
            if ch == "*" and nxt == "/":
                buf.append(nxt)
                i += 2
                in_block_comment = False
                continue
            i += 1
            continue

        if in_single:
            buf.append(ch)
            if ch == "'":
                if nxt == "'":
                    buf.append(nxt)
                    i += 2
                    continue
                in_single = False
            i += 1
            continue

        if in_double:
            buf.append(ch)
            if ch == '"':
                if nxt == '"':
                    buf.append(nxt)
                    i += 2
                    continue
                in_double = False
            i += 1
            continue

        if dollar_tag is not None:
            if sql.startswith(dollar_tag, i):
                buf.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = None
                continue
            buf.append(ch)
            i += 1
            continue

        if ch == "-" and nxt == "-":
            buf.append(ch)
            buf.append(nxt)
            i += 2
            in_line_comment = True
            continue
        if ch == "/" and nxt == "*":
            buf.append(ch)
            buf.append(nxt)
            i += 2
            in_block_comment = True
            continue
        if ch == "'":
            buf.append(ch)
            i += 1
            in_single = True
            continue
        if ch == '"':
            buf.append(ch)
            i += 1
            in_double = True
            continue
        if ch == "$":
            match = _DOLLAR_TAG.match(sql, i)
            if match:
                dollar_tag = match.group(0)
                buf.append(dollar_tag)
                i += len(dollar_tag)
                continue
        if ch == ";":
            flush()
            i += 1
            continue

        buf.append(ch)
        i += 1

    flush()
    return bodies


async def _apply_sql_script(path: Path) -> None:
    sql = path.read_text(encoding="utf-8")  # noqa: ASYNC240 - bootstrap is a one-shot CLI, not a request path
    # Strip transaction wrappers; connection_ctx already commits.
    async with connection_ctx() as conn:
        for body in _statement_bodies(sql):
            try:
                await conn.execute(body)
            except Exception:
                print(f"[bootstrap] statement failed: {body[:80]!r}", flush=True)
                raise


async def main() -> None:
    _ = await init_pool()
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
            except Exception as exc:
                if _is_ignorable_error(exc, _IGNORABLE):
                    print(
                        f"[bootstrap] Patch already present: {sql.strip()[:80]} " + f"({_unwrap_pg_error(exc)})",
                        flush=True,
                    )
                    continue
                # Dirty DBs may already have two tenant memberships per identity.
                if "ux_users_identity_single_tenant" in sql and (
                    isinstance(exc, UniqueViolationError)
                    or isinstance(_unwrap_pg_error(exc), pg_errors.UniqueViolation)
                ):
                    print(
                        "[bootstrap] Skipping ux_users_identity_single_tenant; "
                        + "duplicate identity memberships already exist",
                        flush=True,
                    )
                    continue
                raise
        print("[bootstrap] Done", flush=True)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
