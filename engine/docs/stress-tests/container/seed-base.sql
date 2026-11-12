-- Tenants, users, agents, chat sessions and reply bindings for :agents agents.
-- Ids derive from numbers (agent n is md5('a' || n), its session k is md5('s' || n || '-' || k)), so the pgbench
-- scripts address rows without lookups. Tenant of agent n: (n - 1) % 20 + 1. Owner of agent n: user n.
SET session_replication_role = replica; -- skip foreign-key checks and triggers during the bulk load

INSERT INTO tenants (id, name, slug)
SELECT md5('t' || n)::uuid, 'Stress tenant ' || n, 'stress-' || n
FROM generate_series(1, 20) AS n;

-- A quota no run can exhaust keeps every intake's quota UPDATE taking the user row lock.
INSERT INTO users (id, display_name, tenant_id, quota_message_limit)
SELECT md5('u' || n)::uuid, 'Stress user ' || n, md5('t' || ((n - 1) % 20 + 1))::uuid, 2147483647
FROM generate_series(1, :agents) AS n;

INSERT INTO agents (id, name, role_description, creator_id, tenant_id, status, max_triggers, min_poll_interval_min,
                    webhook_rate_limit, access_mode, company_access_level, heartbeat_interval_minutes)
SELECT md5('a' || n)::uuid, 'Stress agent ' || n, 'stress test', md5('u' || n)::uuid,
       md5('t' || ((n - 1) % 20 + 1))::uuid, 'running', 20, 5, 5, 'company', 'use', 240
FROM generate_series(1, :agents) AS n;

-- Ten web sessions per agent; each is one dispatch lane (conversation).
INSERT INTO chat_sessions (id, agent_id, user_id, title, source_channel, is_primary, created_at, last_message_at)
SELECT md5('s' || n || '-' || k)::uuid, md5('a' || n)::uuid, md5('u' || n)::uuid, 'Session ' || k, 'web', k = 1,
       now() - interval '180 days', now()
FROM generate_series(1, :agents) AS n, generate_series(1, 10) AS k;

INSERT INTO openclaw_reply_bindings (id, agent_id, session_id, owner_id, tenant_id, user_id)
SELECT md5('b' || n || '-' || k)::uuid, md5('a' || n)::uuid, md5('s' || n || '-' || k)::uuid, md5('u' || n)::uuid,
       md5('t' || ((n - 1) % 20 + 1))::uuid, md5('u' || n)::uuid
FROM generate_series(1, :agents) AS n, generate_series(1, 10) AS k;

-- The bulk loads run without secondary indexes; seed-index.sql rebuilds each saved definition afterwards.
CREATE TABLE stress_index_defs AS
SELECT i.indexrelid::regclass::text AS name, pg_get_indexdef(i.indexrelid) AS def
FROM pg_index AS i
WHERE i.indrelid IN ('gateway_messages'::regclass, 'chat_messages'::regclass, 'chat_history_changes'::regclass)
  AND NOT EXISTS (SELECT 1 FROM pg_constraint AS c WHERE c.conindid = i.indexrelid);

SELECT 'DROP INDEX ' || name FROM stress_index_defs \gexec
