-- Chat intake after the fix: identical to bench-intake-before.sql except that chat_session_dao.lock_for_agent now
-- takes the agent row FOR KEY SHARE before the session FOR UPDATE, the order claims and settlements use.
\set n random(1, :agents)
\set k random(1, 5)
\set submission random(1, 4611686018427387904)
BEGIN;
SELECT 1 FROM agents WHERE id = md5('a' || :n)::uuid FOR KEY SHARE;
SELECT * FROM chat_sessions
WHERE id = md5('s' || :n || '-' || :k)::uuid AND agent_id = md5('a' || :n)::uuid FOR UPDATE;
SELECT * FROM chat_messages
WHERE user_id = md5('u' || :n)::uuid AND agent_id = md5('a' || :n)::uuid
  AND conversation_id = md5('s' || :n || '-' || :k)::uuid::text AND client_submission_id = md5('c' || :submission)::uuid;
INSERT INTO chat_messages (id, user_id, agent_id, role, content, conversation_id, client_submission_id,
                           submission_fingerprint, attachments)
VALUES (gen_random_uuid(), md5('u' || :n)::uuid, md5('a' || :n)::uuid, 'user', 'bench',
        md5('s' || :n || '-' || :k)::uuid::text, md5('c' || :submission)::uuid, 'v2:' || repeat('a', 64), '[]')
ON CONFLICT (user_id, agent_id, conversation_id, client_submission_id) WHERE client_submission_id IS NOT NULL
DO NOTHING RETURNING id;
UPDATE users
SET quota_messages_used = CASE WHEN quota_message_period <> 'permanent' AND (quota_period_start IS NULL
            OR now() >= quota_period_start + CASE quota_message_period WHEN 'daily' THEN interval '1 day'
                WHEN 'weekly' THEN interval '7 days' WHEN 'monthly' THEN interval '30 days' END)
        THEN 0 ELSE quota_messages_used END + 1,
    quota_period_start = CASE WHEN quota_message_period <> 'permanent' AND (quota_period_start IS NULL
            OR now() >= quota_period_start + CASE quota_message_period WHEN 'daily' THEN interval '1 day'
                WHEN 'weekly' THEN interval '7 days' WHEN 'monthly' THEN interval '30 days' END)
        THEN now() ELSE quota_period_start END
WHERE id = md5('u' || :n)::uuid
  AND CASE WHEN quota_message_period <> 'permanent' AND (quota_period_start IS NULL
            OR now() >= quota_period_start + CASE quota_message_period WHEN 'daily' THEN interval '1 day'
                WHEN 'weekly' THEN interval '7 days' WHEN 'monthly' THEN interval '30 days' END)
        THEN 0 ELSE quota_messages_used END < quota_message_limit
RETURNING quota_messages_used;
UPDATE chat_sessions SET last_message_at = now() WHERE id = md5('s' || :n || '-' || :k)::uuid;
INSERT INTO gateway_messages (id, agent_id, sender_user_id, conversation_id, content, status)
VALUES (gen_random_uuid(), md5('a' || :n)::uuid, md5('u' || :n)::uuid, md5('s' || :n || '-' || :k)::uuid::text,
        'bench', 'pending');
END;
