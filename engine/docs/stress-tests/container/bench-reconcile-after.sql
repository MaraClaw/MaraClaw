-- One reconciler tick after the fixes. Both first pages start from a row bound below every row
-- (gateway_message_delivery_dao._DISPATCH_START), so the cursor stays an index condition in a generic plan. The
-- pending page reads its scan end (the 1,000th pending row: 50 heads x 20 rows) and then the heads among the next
-- 1,000 pending rows; the delivered page walks ix_gateway_messages_delivered_order.
SELECT g.created_at, g.id FROM gateway_messages g
WHERE g.status = 'pending'
AND (g.created_at, g.id) > ('0001-01-01 00:00:00+00'::timestamptz, '00000000-0000-0000-0000-000000000000'::uuid)
ORDER BY g.created_at, g.id OFFSET 999 LIMIT 1;
SELECT g.* FROM (SELECT s.* FROM gateway_messages s
WHERE s.status = 'pending'
AND (s.created_at, s.id) > ('0001-01-01 00:00:00+00'::timestamptz, '00000000-0000-0000-0000-000000000000'::uuid)
ORDER BY s.created_at, s.id LIMIT 1000) g
LEFT JOIN personal_burst_batches b ON b.gateway_id = g.id
WHERE (b.gateway_id IS NULL OR b.state != 'pending' OR b.ready_at <= clock_timestamp())
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
WHERE g.status = 'delivered' AND g.reply_binding_id IS NOT NULL
AND (g.created_at, g.id) > ('0001-01-01 00:00:00+00'::timestamptz, '00000000-0000-0000-0000-000000000000'::uuid)
ORDER BY g.created_at, g.id LIMIT 51;
