-- One reconciler tick after the fix: the first pending page now starts from a row bound below every row
-- (gateway_message_dao._DISPATCH_START), so it can walk ix_gateway_messages_pending_order. The delivered page is
-- unchanged.
SELECT g.* FROM gateway_messages g
LEFT JOIN personal_burst_batches b ON b.gateway_id = g.id
WHERE g.status = 'pending'
AND (g.created_at, g.id) > ('0001-01-01 00:00:00+00'::timestamptz, '00000000-0000-0000-0000-000000000000'::uuid)
AND (b.gateway_id IS NULL OR b.state != 'pending' OR b.ready_at <= clock_timestamp())
AND (g.conversation_id IS NULL OR g.conversation_id = '' OR (
NOT EXISTS (SELECT 1 FROM gateway_messages d WHERE d.agent_id = g.agent_id
AND d.conversation_id = g.conversation_id AND d.status = 'delivered')
AND NOT EXISTS (SELECT 1 FROM gateway_messages p WHERE p.agent_id = g.agent_id
AND p.conversation_id = g.conversation_id AND p.status = 'pending'
AND (p.created_at, p.id) < (g.created_at, g.id))))
ORDER BY g.created_at, g.id LIMIT 51;
SELECT g.* FROM gateway_messages g
JOIN agents a ON a.id = g.agent_id
JOIN openclaw_reply_bindings b ON b.id = g.reply_binding_id AND b.agent_id = g.agent_id
AND b.owner_id = a.creator_id AND b.tenant_id IS NOT DISTINCT FROM a.tenant_id
WHERE g.status = 'delivered' AND (NULL::timestamptz IS NULL
OR (g.created_at, g.id) > (NULL::timestamptz, NULL::uuid))
ORDER BY g.created_at, g.id LIMIT 51;
