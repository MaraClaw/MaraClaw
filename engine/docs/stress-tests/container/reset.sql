-- Restores the steady state after a pgbench run: removes the rows the bench wrote and requeues the seeded turns.
SET session_replication_role = replica; -- skip the history-revision and reply-binding revocation triggers

DELETE FROM chat_history_changes AS h USING chat_messages AS m
WHERE h.message_id = m.id AND m.content IN ('bench', 'bench reply');
DELETE FROM chat_messages WHERE content IN ('bench', 'bench reply');
DELETE FROM gateway_messages WHERE content = 'bench';
UPDATE gateway_messages
SET status = 'pending', result = NULL, delivered_at = NULL, completed_at = NULL, reply_binding_id = NULL
WHERE (content LIKE 'pending %' OR content = 'queued behind delivered') AND status <> 'pending';
UPDATE users SET quota_messages_used = 0, quota_period_start = NULL WHERE quota_messages_used <> 0;

RESET session_replication_role;
VACUUM ANALYZE gateway_messages;
VACUUM ANALYZE chat_messages;
VACUUM ANALYZE chat_history_changes;
VACUUM ANALYZE chat_sessions;
VACUUM ANALYZE users;
VACUUM ANALYZE agents;
