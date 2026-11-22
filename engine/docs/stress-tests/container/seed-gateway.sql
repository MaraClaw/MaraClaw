-- :history completed turns per agent. Rows are written in time order across agents, as production traffic arrives,
-- so one conversation's rows spread over many heap pages. Turn i of agent n belongs to session i % 10 + 1. Session 6
-- is the colleague's canonical conversation, so the colleague sends its turns; only the canonical web sessions 1 and 6
-- carry a reply binding (bind_openclaw_reply binds 'web' rows only).
SET session_replication_role = replica;

INSERT INTO gateway_messages (id, agent_id, sender_user_id, conversation_id, content, status, result,
                              reply_binding_id, created_at, delivered_at, completed_at)
SELECT gen_random_uuid(), md5('a' || n)::uuid, md5('u' || CASE i % 10 + 1 WHEN 6 THEN n + :agents ELSE n END)::uuid,
       md5('s' || n || '-' || (i % 10 + 1))::uuid::text, 'question ' || i, 'completed', 'answer ' || i,
       CASE WHEN i % 10 + 1 IN (1, 6) THEN md5('b' || n || '-' || (i % 10 + 1))::uuid END,
       t.at, t.at, t.at + interval '20 seconds'
FROM generate_series(1, :history) AS i
CROSS JOIN LATERAL generate_series(1 + 0 * i, :agents) AS n -- the reference to i keeps time as the outer loop
CROSS JOIN LATERAL (SELECT now() - interval '180 days' + i * interval '3110 milliseconds' AS at) AS t;
