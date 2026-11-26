-- Current org-member intake, database-only. All seed senders are members of the agent's tiered tenant;
-- the daily limit is a fixture input, and all principals/agents/sessions have already passed eligibility checks.
\set n random(1, :agents)
\set t (:n - 1) % 20 + 1
\set k random(1, 5)
\set submission random(1, 4611686018427387904)
BEGIN;
SELECT 1 FROM agents WHERE id = md5('a' || :n)::uuid FOR KEY SHARE;
-- Seeded sessions are still titled "Session k", the state in which accept_submission asks the first-message probe below.
SELECT (title LIKE 'Session %')::int AS untitled FROM chat_sessions
WHERE id = md5('s' || :n || '-' || :k)::uuid AND agent_id = md5('a' || :n)::uuid FOR UPDATE \gset

-- A matched fingerprint is a replay; a different one is a refusal. Neither runs the quota checks.
SELECT COALESCE((SELECT CASE WHEN submission_fingerprint = 'v1:' || repeat('a', 64) THEN 1 ELSE -1 END
    FROM chat_messages WHERE user_id = md5('u' || :n)::uuid AND agent_id = md5('a' || :n)::uuid
      AND conversation_id = md5('s' || :n || '-' || :k)::uuid::text
      AND client_submission_id = md5('c' || :submission)::uuid), 0) AS existing \gset
\set reserved 0
\if :existing = 0
-- chat_message_dao.conversation_has_messages, which replaced the transcript read: one index probe, asked only for a
-- session that is still untitled and before the reservation, so the new message cannot count.
\if :untitled
SELECT EXISTS (SELECT 1 FROM chat_messages WHERE agent_id = md5('a' || :n)::uuid
    AND conversation_id = md5('s' || :n || '-' || :k)::uuid::text) AS has_messages;
\endif
WITH reservation AS (
    INSERT INTO chat_messages (id, user_id, agent_id, role, content, conversation_id, client_submission_id,
                               submission_fingerprint, attachments)
    VALUES (gen_random_uuid(), md5('u' || :n)::uuid, md5('a' || :n)::uuid, 'user', 'bench',
            md5('s' || :n || '-' || :k)::uuid::text, md5('c' || :submission)::uuid, 'v1:' || repeat('a', 64), '[]')
    ON CONFLICT (user_id, agent_id, conversation_id, client_submission_id) WHERE client_submission_id IS NOT NULL
    DO NOTHING RETURNING id)
SELECT count(*) AS reserved FROM reservation \gset
\if :reserved = 0
-- The session lock serializes same-session benchmark clients; keep the live path's post-conflict reread anyway.
SELECT COALESCE((SELECT CASE WHEN submission_fingerprint = 'v1:' || repeat('a', 64) THEN 1 ELSE -1 END
    FROM chat_messages WHERE user_id = md5('u' || :n)::uuid AND agent_id = md5('a' || :n)::uuid
      AND conversation_id = md5('s' || :n || '-' || :k)::uuid::text
      AND client_submission_id = md5('c' || :submission)::uuid), 0) AS existing \gset
\endif
\endif

\if :reserved
-- charge_conversation_quota reads the member's tenant tier; tiered members never update users.
SELECT tier FROM tenants WHERE id = md5('t' || :t)::uuid;
-- ensure_workspace_within_quota checks the persisted overage marker, not a live measurement.
SELECT (NOT EXISTS (SELECT 1 FROM agent_workspace_usage
    WHERE agent_id = md5('a' || :n)::uuid AND over_since IS NOT NULL))::int AS workspace_ok \gset
\set charged 0
\if :workspace_ok
-- charge_agent_daily_message reads the agent's tier and the genesis org admin's timezone, not the sender's
-- timezone or tenants.timezone. The fixture uses valid IANA names; missing/empty timezone falls back to UTC.
SELECT tier FROM tenants WHERE id = md5('t' || :t)::uuid;
SELECT (clock_timestamp() AT TIME ZONE COALESCE((SELECT NULLIF(timezone, '') FROM users
    WHERE tenant_id = md5('t' || :t)::uuid AND role = 'org_admin' AND is_genesis IS TRUE LIMIT 1), 'UTC'))::date
    AS usage_date \gset
WITH charge AS (
    INSERT INTO agent_daily_message_usage AS usage (agent_id, usage_date, messages_used)
    VALUES (md5('a' || :n)::uuid, :usage_date, 1)
    ON CONFLICT (agent_id) DO UPDATE
    SET usage_date = EXCLUDED.usage_date,
        messages_used = CASE WHEN usage.usage_date = EXCLUDED.usage_date THEN usage.messages_used + 1 ELSE 1 END,
        updated_at = now()
    WHERE usage.usage_date <> EXCLUDED.usage_date OR usage.messages_used < :daily_message_limit
    RETURNING messages_used)
SELECT count(*) AS charged FROM charge \gset
\endif
\if :charged
UPDATE chat_sessions SET last_message_at = clock_timestamp() WHERE id = md5('s' || :n || '-' || :k)::uuid;
INSERT INTO gateway_messages (id, agent_id, sender_user_id, conversation_id, content, status)
VALUES (gen_random_uuid(), md5('a' || :n)::uuid, md5('u' || :n)::uuid, md5('s' || :n || '-' || :k)::uuid::text,
        'bench', 'pending');
COMMIT;
\else
ROLLBACK;
\endif
\elif :existing = 1
COMMIT;
\else
ROLLBACK;
\endif
