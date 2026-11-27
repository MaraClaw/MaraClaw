-- A recent-history read (chat_message_dao.list_recent). Intake and the chat socket no longer issue it; it remains
-- background read pressure on chat_messages and a guard for the index that serves it.
\set n random(1, :agents)
\set k random(1, 10)
SELECT * FROM chat_messages
WHERE agent_id = md5('a' || :n)::uuid AND conversation_id = md5('s' || :n || '-' || :k)::uuid::text
ORDER BY created_at DESC LIMIT 50;
