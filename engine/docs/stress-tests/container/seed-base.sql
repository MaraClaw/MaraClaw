-- Tenants, users, agents, chat sessions and reply bindings for :agents agents.
-- Ids derive from numbers (agent n is md5('a' || n), its session k is md5('s' || n || '-' || k)), so the pgbench
-- scripts address rows without lookups. Tenant of agent n: (n - 1) % 20 + 1. Owner of agent n: user n. Colleague of
-- agent n: user n + :agents, in the same tenant.
SET session_replication_role = replica; -- skip foreign-key checks and triggers during the bulk load

INSERT INTO tenants (id, name, slug, tier)
SELECT md5('t' || n)::uuid, 'Stress tenant ' || n, 'stress-' || n, 'ultra'
FROM generate_series(1, 20) AS n;

-- A quota no run can exhaust keeps every intake's quota UPDATE taking the user row lock.
INSERT INTO users (id, display_name, tenant_id, quota_message_limit)
SELECT md5('u' || n)::uuid, 'Stress user ' || n, md5('t' || ((n - 1) % :agents % 20 + 1))::uuid, 2147483647
FROM generate_series(1, 2 * :agents) AS n;

INSERT INTO agents (id, name, role_description, creator_id, tenant_id, status, max_triggers, min_poll_interval_min,
                    webhook_rate_limit, access_mode, company_access_level, heartbeat_interval_minutes)
SELECT md5('a' || n)::uuid, 'Stress agent ' || n, 'stress test', md5('u' || n)::uuid,
       md5('t' || ((n - 1) % 20 + 1))::uuid, 'running', 20, 5, 5, 'company', 'use', 240
FROM generate_series(1, :agents) AS n;

-- Ten sessions per agent; each is one dispatch lane (conversation). Sessions 1 and 6 are the canonical web
-- conversations of the owner and the colleague (chat_session_dao.insert_primary_platform: 'web', primary, not a
-- group), so each is a human lane. Sessions 2, 3, 7 and 8 are IM group chats and 4, 5, 9 and 10 agent-to-agent
-- sessions with other agents of the tenant: ordinary lanes.
INSERT INTO chat_sessions (id, agent_id, user_id, title, source_channel, external_conv_id, is_group, group_name,
                           peer_agent_id, is_primary, created_at, last_message_at)
SELECT md5('s' || n || '-' || k)::uuid, md5('a' || n)::uuid, md5('u' || CASE k WHEN 6 THEN n + :agents ELSE n END)::uuid,
       'Session ' || k,
       CASE WHEN k IN (1, 6) THEN 'web' WHEN k IN (2, 3, 7, 8) THEN 'telegram' ELSE 'agent' END,
       CASE WHEN k IN (2, 3, 7, 8) THEN 'stress-group-' || n || '-' || k END,
       k IN (2, 3, 7, 8),
       CASE WHEN k IN (2, 3, 7, 8) THEN 'Stress group ' || k END,
       CASE WHEN k IN (4, 5, 9, 10) THEN md5('a' || ((n - 1 + 20 * (k % 5 + 1)) % :agents + 1))::uuid END,
       k IN (1, 6), now() - interval '180 days', now()
FROM generate_series(1, :agents) AS n, generate_series(1, 10) AS k;

-- bind_openclaw_reply binds canonical web sessions only, so only the two human lanes carry a reply binding.
INSERT INTO openclaw_reply_bindings (id, agent_id, session_id, owner_id, tenant_id, user_id)
SELECT md5('b' || n || '-' || k)::uuid, md5('a' || n)::uuid, md5('s' || n || '-' || k)::uuid, md5('u' || n)::uuid,
       md5('t' || ((n - 1) % 20 + 1))::uuid, md5('u' || CASE k WHEN 6 THEN n + :agents ELSE n END)::uuid
FROM generate_series(1, :agents) AS n, unnest(ARRAY[1, 6]) AS k;

-- The bulk loads run without secondary indexes; seed-index.sql rebuilds each saved definition afterwards.
CREATE TABLE stress_index_defs AS
SELECT i.indexrelid::regclass::text AS name, pg_get_indexdef(i.indexrelid) AS def
FROM pg_index AS i
WHERE i.indrelid IN ('gateway_messages'::regclass, 'chat_messages'::regclass, 'chat_history_changes'::regclass)
  AND NOT EXISTS (SELECT 1 FROM pg_constraint AS c WHERE c.conindid = i.indexrelid);

SELECT 'DROP INDEX ' || name FROM stress_index_defs \gexec
