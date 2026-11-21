-- :history chat messages per agent, alternating user and assistant turns, in the same lanes and time order as
-- seed-gateway.sql. Session 6 is the colleague's canonical conversation, so its rows carry the colleague's user id.
SET session_replication_role = replica;

INSERT INTO chat_messages (id, agent_id, user_id, role, content, conversation_id, created_at)
SELECT gen_random_uuid(), md5('a' || n)::uuid, md5('u' || CASE i % 10 + 1 WHEN 6 THEN n + :agents ELSE n END)::uuid,
       (CASE WHEN (i / 10) % 2 = 0 THEN 'user' ELSE 'assistant' END)::chat_role_enum,
       'message body ' || i, md5('s' || n || '-' || (i % 10 + 1))::uuid::text,
       now() - interval '180 days' + i * interval '3110 milliseconds'
FROM generate_series(1, :history) AS i
CROSS JOIN LATERAL generate_series(1 + 0 * i, :agents) AS n;
