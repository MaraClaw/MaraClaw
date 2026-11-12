-- One history revision per seeded chat message, as the refresh_chat_history_revision trigger writes them.
SET session_replication_role = replica;

INSERT INTO chat_history_changes (session_id, revision, previous_revision, message_id)
SELECT md5('s' || n || '-' || (i % 10 + 1))::uuid, gen_random_uuid(), gen_random_uuid(), gen_random_uuid()
FROM generate_series(1, :history) AS i
CROSS JOIN LATERAL generate_series(1 + 0 * i, :agents) AS n;
