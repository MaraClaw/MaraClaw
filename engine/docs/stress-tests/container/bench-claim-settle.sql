-- A native claim of one lane head (openclaw_native_claims.claim_ordinary_turn) and, when it wins, its settlement
-- (openclaw_gateway_delivery.settle_native_report -> record_report). Both lock the tenant and owner (shared) and
-- the agent (exclusive) first; bind_openclaw_reply and the assistant INSERT's history trigger then lock the
-- session. Lane 6 always holds a delivered turn, so claims there find no head and stop after the locks.
\set n random(1, :agents)
\set t (:n - 1) % 20 + 1
\set k random(1, 6)
BEGIN;
SELECT id FROM tenants WHERE id = md5('t' || :t)::uuid FOR SHARE;
SELECT tenant_id FROM users WHERE id = md5('u' || :n)::uuid FOR KEY SHARE;
SELECT tenant_id FROM agents WHERE id = md5('a' || :n)::uuid FOR UPDATE;
SELECT coalesce((SELECT id FROM gateway_messages
                 WHERE agent_id = md5('a' || :n)::uuid AND conversation_id = md5('s' || :n || '-' || :k)::uuid::text
                   AND status = 'pending' ORDER BY created_at, id LIMIT 1),
                '00000000-0000-0000-0000-000000000000') AS head \gset
WITH claimed AS (
    UPDATE gateway_messages g SET status = 'delivered', delivered_at = clock_timestamp(),
        reply_binding_id = bind_openclaw_reply(g.agent_id, g.conversation_id)
    WHERE g.id = :head AND g.agent_id = md5('a' || :n)::uuid AND g.status = 'pending'
      AND (g.conversation_id IS NULL OR g.conversation_id = '' OR (
          NOT EXISTS (SELECT 1 FROM gateway_messages d WHERE d.agent_id = g.agent_id
              AND d.conversation_id = g.conversation_id AND d.status = 'delivered')
          AND NOT EXISTS (SELECT 1 FROM gateway_messages p WHERE p.agent_id = g.agent_id
              AND p.conversation_id = g.conversation_id AND p.status = 'pending'
              AND (p.created_at, p.id) < (g.created_at, g.id))))
    RETURNING g.id)
SELECT count(*) AS claimed FROM claimed \gset
END;
\if :claimed
BEGIN;
SELECT id FROM tenants WHERE id = md5('t' || :t)::uuid FOR SHARE;
SELECT tenant_id FROM users WHERE id = md5('u' || :n)::uuid FOR KEY SHARE;
SELECT tenant_id FROM agents WHERE id = md5('a' || :n)::uuid FOR UPDATE;
UPDATE gateway_messages SET status = 'completed', result = 'bench reply', completed_at = now()
WHERE id = :head AND agent_id = md5('a' || :n)::uuid AND status IN ('pending', 'delivered') RETURNING id;
INSERT INTO chat_messages (id, agent_id, user_id, role, content, conversation_id)
VALUES (gen_random_uuid(), md5('a' || :n)::uuid, md5('u' || :n)::uuid, 'assistant', 'bench reply',
        md5('s' || :n || '-' || :k)::uuid::text);
UPDATE agents SET openclaw_last_seen = now() WHERE id = md5('a' || :n)::uuid;
END;
\endif
