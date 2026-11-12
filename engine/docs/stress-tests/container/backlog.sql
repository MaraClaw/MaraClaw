-- :add = 1 queues :backlog more pending turns per agent on lanes 7-10 (81 per agent gives 184,000 pending at 2,000
-- agents); :add = 0 removes them again.
\if :add
INSERT INTO gateway_messages (id, agent_id, sender_user_id, conversation_id, content, status, created_at)
SELECT gen_random_uuid(), md5('a' || n)::uuid, md5('u' || n)::uuid, md5('s' || n || '-' || (7 + j % 4))::uuid::text,
       'backlog', 'pending', now() - interval '3 minutes' + j * interval '1 millisecond' + n * interval '1 microsecond'
FROM generate_series(1, :agents) AS n, generate_series(1, :backlog) AS j;
\else
DELETE FROM gateway_messages WHERE content = 'backlog';
\endif

VACUUM ANALYZE gateway_messages;
