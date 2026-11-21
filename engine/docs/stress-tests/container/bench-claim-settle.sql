-- A native claim of one lane head and, when it wins, its settlement (openclaw_gateway_delivery.settle_native_report
-- -> record_report). Lanes 1 and 6 are canonical web sessions, so their claims take the human-lane path
-- (openclaw_shared_dispatch.claim_shared_turn); lanes 2 to 5 are group and agent-to-agent sessions on the ordinary
-- path (openclaw_native_claims.claim_ordinary_turn). Lane 6 always holds a delivered turn: a human claim there sees
-- the active lane in its unlocked head read and stops before any lock.
\set n random(1, :agents)
\set t (:n - 1) % 20 + 1
\set k random(1, 6)
\set human case when :k = 1 or :k = 6 then 1 else 0 end
\set member case when :k = 6 then :n + :agents else :n end
\set claimed 0
BEGIN;
\if :human
-- personal_burst_dispatch_dao.head, read before any lock.
SELECT coalesce(h.id, '00000000-0000-0000-0000-000000000000') AS head, coalesce(h.ready, 0) AS ready
FROM (SELECT 1) AS one LEFT JOIN LATERAL (
    SELECT g.id, (NOT EXISTS (SELECT 1 FROM gateway_messages d WHERE d.agent_id = g.agent_id
                      AND d.conversation_id = g.conversation_id AND d.status = 'delivered'))::int AS ready
    FROM gateway_messages g LEFT JOIN personal_burst_batches b ON b.gateway_id = g.id
    WHERE g.agent_id = md5('a' || :n)::uuid AND g.conversation_id = md5('s' || :n || '-' || :k)::uuid::text
      AND g.status = 'pending' ORDER BY g.created_at, g.id LIMIT 1) AS h ON true \gset
\if :ready
-- openclaw_shared_scope.authorize_lane takes shared tenant, owner and agent locks; then
-- gateway_message_dao.lock_human_lane locks the canonical session and the head is read again under it.
SELECT id FROM tenants WHERE id = md5('t' || :t)::uuid FOR SHARE;
SELECT id FROM users WHERE id = md5('u' || :n)::uuid FOR SHARE;
SELECT id FROM agents WHERE id = md5('a' || :n)::uuid FOR SHARE;
SELECT id FROM chat_sessions WHERE id = md5('s' || :n || '-' || :k)::uuid AND agent_id = md5('a' || :n)::uuid
  AND source_channel = 'web' AND is_primary IS TRUE AND is_group IS FALSE FOR UPDATE;
SELECT coalesce((SELECT g.id FROM gateway_messages g LEFT JOIN personal_burst_batches b ON b.gateway_id = g.id
                 WHERE g.agent_id = md5('a' || :n)::uuid
                   AND g.conversation_id = md5('s' || :n || '-' || :k)::uuid::text AND g.status = 'pending'
                 ORDER BY g.created_at, g.id LIMIT 1),
                '00000000-0000-0000-0000-000000000000') AS head \gset
-- gateway_message_dao.claim_head: bind_openclaw_reply binds this canonical web session.
WITH claimed AS (
    UPDATE gateway_messages g SET status = 'delivered', delivered_at = clock_timestamp(), content = g.content,
        selected_slot = g.selected_slot, guest_model_ref = g.guest_model_ref, routing_reason = g.routing_reason,
        reply_binding_id = bind_openclaw_reply(g.agent_id, g.conversation_id)
    WHERE g.id = :head AND g.agent_id = md5('a' || :n)::uuid
      AND g.conversation_id = md5('s' || :n || '-' || :k)::uuid::text AND g.status = 'pending'
      AND NOT EXISTS (SELECT 1 FROM gateway_messages d WHERE d.agent_id = g.agent_id
          AND d.conversation_id = g.conversation_id AND d.status = 'delivered')
      AND NOT EXISTS (SELECT 1 FROM gateway_messages o WHERE o.agent_id = g.agent_id
          AND o.conversation_id = g.conversation_id AND o.status = 'pending'
          AND (o.created_at, o.id) < (g.created_at, g.id))
    RETURNING g.id)
SELECT count(*) AS claimed FROM claimed \gset
\endif
\else
-- Ordinary lane: shared tenant and owner locks, the exclusive agent lock, then gateway_message_dao.claim_ordinary_head.
-- Its bind_openclaw_reply finds no canonical web session for a group or agent conversation and returns NULL.
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
\endif
END;
\if :claimed
BEGIN;
SELECT id FROM tenants WHERE id = md5('t' || :t)::uuid FOR SHARE;
SELECT tenant_id FROM users WHERE id = md5('u' || :n)::uuid FOR KEY SHARE;
SELECT tenant_id FROM agents WHERE id = md5('a' || :n)::uuid FOR UPDATE;
\if :human
-- record_report on a human lane: the lane lock, then the Personal burst header lookup (none for a web turn).
SELECT id FROM chat_sessions WHERE id = md5('s' || :n || '-' || :k)::uuid AND agent_id = md5('a' || :n)::uuid
  AND source_channel = 'web' AND is_primary IS TRUE AND is_group IS FALSE FOR UPDATE;
SELECT agent_id, transcript_session_id FROM personal_burst_batches WHERE gateway_id = :head;
\endif
UPDATE gateway_messages SET status = 'completed', result = 'bench reply', completed_at = now()
WHERE id = :head AND agent_id = md5('a' || :n)::uuid AND status IN ('pending', 'delivered') RETURNING id;
INSERT INTO chat_messages (id, agent_id, user_id, role, content, conversation_id)
VALUES (gen_random_uuid(), md5('a' || :n)::uuid, md5('u' || :member)::uuid, 'assistant', 'bench reply',
        md5('s' || :n || '-' || :k)::uuid::text);
UPDATE agents SET openclaw_last_seen = now() WHERE id = md5('a' || :n)::uuid;
END;
\endif
