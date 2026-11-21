-- EXPLAIN probes for :variant (before | after) in :phase (steady | backlog), recorded through stress_explain().
-- The pending pages mirror gateway_message_dao.pending_dispatch_page and the delivered pages
-- gateway_message_delivery_dao.delivered_dispatch_page, before and after the fixes; history and gateway-list mirror
-- chat_message_dao.list_recent and gateway_message_dao.list_recent for one agent.
SELECT :'variant' = 'after' AS is_after, round(:agents * 0.617) AS probe_agent \gset

-- Cursors: the middle lane head, so the page still has heads ahead of it as most reconciler ticks do, and the last
-- lane head. The page after the last head is the slowest one, because every row behind it fails the head test.
WITH heads AS (
    SELECT g.created_at, g.id, row_number() OVER (ORDER BY g.created_at, g.id) AS ord, count(*) OVER () AS total
    FROM gateway_messages AS g
    WHERE g.status = 'pending'
      AND NOT EXISTS (SELECT 1 FROM gateway_messages AS d WHERE d.agent_id = g.agent_id
          AND d.conversation_id = g.conversation_id AND d.status = 'delivered')
      AND NOT EXISTS (SELECT 1 FROM gateway_messages AS p WHERE p.agent_id = g.agent_id
          AND p.conversation_id = g.conversation_id AND p.status = 'pending'
          AND (p.created_at, p.id) < (g.created_at, g.id))
)
SELECT m.created_at AS mid_at, m.id AS mid_id, t.created_at AS tail_at, t.id AS tail_id
FROM heads AS m, heads AS t
WHERE m.ord = m.total / 2 + 1 AND t.ord = t.total \gset

PREPARE stress_pending_before (timestamptz, uuid) AS
SELECT g.* FROM gateway_messages g
LEFT JOIN personal_burst_batches b ON b.gateway_id = g.id
WHERE g.status = 'pending' AND ($1::timestamptz IS NULL OR (g.created_at, g.id) > ($1, $2))
AND (b.gateway_id IS NULL OR b.state != 'pending' OR b.ready_at <= clock_timestamp())
AND (g.conversation_id IS NULL OR g.conversation_id = '' OR (
NOT EXISTS (SELECT 1 FROM gateway_messages d WHERE d.agent_id = g.agent_id
AND d.conversation_id = g.conversation_id AND d.status = 'delivered')
AND NOT EXISTS (SELECT 1 FROM gateway_messages p WHERE p.agent_id = g.agent_id
AND p.conversation_id = g.conversation_id AND p.status = 'pending'
AND (p.created_at, p.id) < (g.created_at, g.id))))
ORDER BY g.created_at, g.id LIMIT 51;

-- The after page reads at most 1,000 pending rows (50 heads x 20 rows) behind its cursor, so the page after the last
-- head stops there; stress_pending_scan_end is its first statement, which finds the cursor for that case.
PREPARE stress_pending_scan_end (timestamptz, uuid) AS
SELECT g.created_at, g.id FROM gateway_messages g
WHERE g.status = 'pending' AND (g.created_at, g.id) > ($1, $2)
ORDER BY g.created_at, g.id OFFSET 999 LIMIT 1;

PREPARE stress_pending_after (timestamptz, uuid) AS
SELECT g.* FROM (SELECT s.* FROM gateway_messages s
WHERE s.status = 'pending' AND (s.created_at, s.id) > ($1, $2)
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

PREPARE stress_delivered_before (timestamptz, uuid) AS
SELECT g.* FROM gateway_messages g
JOIN agents a ON a.id = g.agent_id
JOIN openclaw_reply_bindings b ON b.id = g.reply_binding_id AND b.agent_id = g.agent_id
AND b.owner_id = a.creator_id AND b.tenant_id IS NOT DISTINCT FROM a.tenant_id
WHERE g.status = 'delivered' AND ($1::timestamptz IS NULL OR (g.created_at, g.id) > ($1, $2))
ORDER BY g.created_at, g.id LIMIT 51;

PREPARE stress_delivered_after (timestamptz, uuid) AS
SELECT g.* FROM gateway_messages g
JOIN agents a ON a.id = g.agent_id
JOIN openclaw_reply_bindings b ON b.id = g.reply_binding_id AND b.agent_id = g.agent_id
AND b.owner_id = a.creator_id AND b.tenant_id IS NOT DISTINCT FROM a.tenant_id
WHERE g.status = 'delivered' AND g.reply_binding_id IS NOT NULL AND (g.created_at, g.id) > ($1, $2)
ORDER BY g.created_at, g.id LIMIT 51;

\if :is_after
\set statement stress_pending_after
\set delivered stress_delivered_after
\set first_page '''0001-01-01 00:00:00+00'', ''00000000-0000-0000-0000-000000000000'''
\else
\set statement stress_pending_before
\set delivered stress_delivered_before
\set first_page 'NULL, NULL'
\endif

SET plan_cache_mode = force_custom_plan;
SELECT stress_explain(:'variant', :'phase', 'pending-page-1', format('EXECUTE %s(%s)', :'statement', :'first_page'));
SELECT stress_explain(:'variant', :'phase', 'pending-page-mid',
                      format('EXECUTE %s(%L, %L)', :'statement', :'mid_at', :'mid_id'));
SELECT stress_explain(:'variant', :'phase', 'pending-page-tail',
                      format('EXECUTE %s(%L, %L)', :'statement', :'tail_at', :'tail_id'));
\if :is_after
SELECT stress_explain(:'variant', :'phase', 'pending-scan-end-tail',
                      format('EXECUTE stress_pending_scan_end(%L, %L)', :'tail_at', :'tail_id'));
\endif
SELECT stress_explain(:'variant', :'phase', 'delivered-page-1', format('EXECUTE %s(%s)', :'delivered', :'first_page'));

-- psycopg prepares a query server-side from its fifth execution, and PostgreSQL may then reuse one generic plan for
-- every cursor value.
SET plan_cache_mode = force_generic_plan;
SELECT stress_explain(:'variant', :'phase', 'pending-page-mid-generic',
                      format('EXECUTE %s(%L, %L)', :'statement', :'mid_at', :'mid_id'));
SELECT stress_explain(:'variant', :'phase', 'pending-page-tail-generic',
                      format('EXECUTE %s(%L, %L)', :'statement', :'tail_at', :'tail_id'));
SELECT stress_explain(:'variant', :'phase', 'delivered-page-1-generic',
                      format('EXECUTE %s(%s)', :'delivered', :'first_page'));
\if :is_after
-- The pre-fix SQL on the fixed schema: in a generic plan its cursor cannot become an index condition.
SELECT stress_explain(:'variant', :'phase', 'pending-page-mid-generic-old-sql',
                      format('EXECUTE stress_pending_before(%L, %L)', :'mid_at', :'mid_id'));
\endif
RESET plan_cache_mode;

SELECT stress_explain(:'variant', :'phase', 'history', format(
    'SELECT * FROM chat_messages WHERE agent_id = %L AND conversation_id = %L ORDER BY created_at DESC LIMIT 50',
    md5('a' || :probe_agent)::uuid, md5('s' || :probe_agent || '-7')::uuid::text));
SELECT stress_explain(:'variant', :'phase', 'gateway-list', format(
    'SELECT * FROM gateway_messages WHERE agent_id = %L ORDER BY created_at DESC NULLS LAST LIMIT 50',
    md5('a' || :probe_agent)::uuid));

DEALLOCATE ALL;
