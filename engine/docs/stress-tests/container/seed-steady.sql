-- Steady state per agent: lanes 1-5 hold two pending turns each, and lane 6 has a turn in flight with one queued
-- behind it. At 2,000 agents that is 22,000 pending and 2,000 delivered rows.
INSERT INTO gateway_messages (id, agent_id, sender_user_id, conversation_id, content, status, created_at)
SELECT gen_random_uuid(), md5('a' || n)::uuid, md5('u' || n)::uuid, md5('s' || n || '-' || k)::uuid::text,
       'pending ' || k || '.' || j, 'pending',
       now() - interval '10 minutes' + j * interval '1 second' + n * interval '1 millisecond'
FROM generate_series(1, :agents) AS n, generate_series(1, 5) AS k, generate_series(1, 2) AS j;

INSERT INTO gateway_messages (id, agent_id, sender_user_id, conversation_id, content, status, reply_binding_id,
                              created_at, delivered_at)
SELECT gen_random_uuid(), md5('a' || n)::uuid, md5('u' || n)::uuid, md5('s' || n || '-6')::uuid::text,
       'in flight', 'delivered', md5('b' || n || '-6')::uuid, now() - interval '5 minutes', now() - interval '5 minutes'
FROM generate_series(1, :agents) AS n;

INSERT INTO gateway_messages (id, agent_id, sender_user_id, conversation_id, content, status, created_at)
SELECT gen_random_uuid(), md5('a' || n)::uuid, md5('u' || n)::uuid, md5('s' || n || '-6')::uuid::text,
       'queued behind delivered', 'pending', now() - interval '4 minutes' + n * interval '1 millisecond'
FROM generate_series(1, :agents) AS n;
