-- Prints the stress-test report and fills stress_check; any FAIL row makes stress.sh exit non-zero.
\pset footer off
\pset null '-'

CREATE FUNCTION stress_walks(plan jsonb, index_name text) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $fn$
    SELECT jsonb_path_exists(plan, '$.** ? (@."Index Name" == $index && exists (@."Index Cond"))',
                             jsonb_build_object('index', index_name))
$fn$;

CREATE FUNCTION stress_has(plan jsonb, node_type text) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $fn$
    SELECT jsonb_path_exists(plan, '$.** ? (@."Node Type" == $node)', jsonb_build_object('node', node_type))
$fn$;

CREATE VIEW stress_plan_summary AS
SELECT variant, phase, label, pending,
       round((plan -> 0 ->> 'Execution Time')::numeric, 2) AS ms,
       (plan -> 0 -> 'Plan' ->> 'Shared Hit Blocks')::bigint
           + (plan -> 0 -> 'Plan' ->> 'Shared Read Blocks')::bigint AS buffers,
       concat_ws(' + ',
           (SELECT string_agg(DISTINCT idx.value #>> '{}', ', ')
            FROM jsonb_path_query(plan, '$.**."Index Name"') AS idx(value)),
           CASE WHEN stress_has(plan, 'Seq Scan') THEN 'Seq Scan' END,
           CASE WHEN stress_has(plan, 'Sort') THEN 'Sort' END) AS shape
FROM stress_plan;

-- pgbench log time is microseconds, or the failure kind ('deadlock', 'serialization', 'failed').
CREATE VIEW stress_tx_parsed AS
SELECT variant,
       CASE WHEN kind = 'rec' THEN 'reconciler tick' WHEN script = '0' THEN 'intake'
            WHEN script = '1' THEN 'claim + settle' ELSE 'history read' END AS workload,
       CASE WHEN kind = 'rec' THEN 4 ELSE script::integer + 1 END AS ord,
       CASE WHEN t ~ '^[0-9]+$' THEN t::bigint END AS us,
       t
FROM (SELECT variant, kind, split_part(line, ' ', 3) AS t, split_part(line, ' ', 4) AS script FROM stress_tx) AS raw;

\echo
\echo '== Scale and timings'
SELECT key, value FROM stress_meta ORDER BY ord;

\echo
\echo '== Query plans (second warm EXPLAIN ANALYZE run; buffers = shared hit + read)'
SELECT phase, label AS probe, coalesce(b.pending, a.pending) AS pending,
       b.ms AS before_ms, a.ms AS after_ms, b.buffers AS before_buffers, a.buffers AS after_buffers,
       b.shape AS before_plan, a.shape AS after_plan
FROM (SELECT * FROM stress_plan_summary WHERE variant = 'before') AS b
FULL JOIN (SELECT * FROM stress_plan_summary WHERE variant = 'after') AS a USING (phase, label)
ORDER BY phase DESC, array_position(ARRAY['pending-page-1', 'pending-page-mid', 'pending-page-tail',
    'pending-page-mid-generic', 'pending-page-mid-generic-old-sql', 'history', 'gateway-list'], label);

\echo
\echo '== Mixed workload: ms per pgbench transaction (the reconciler tick includes its schedule lag)'
SELECT workload, variant, count(us) AS completed,
       count(*) FILTER (WHERE t = 'deadlock') AS deadlocks,
       count(*) FILTER (WHERE us IS NULL AND t <> 'deadlock') AS other_failures,
       round((percentile_cont(0.5) WITHIN GROUP (ORDER BY us) / 1000)::numeric, 1) AS p50,
       round((percentile_cont(0.95) WITHIN GROUP (ORDER BY us) / 1000)::numeric, 1) AS p95,
       round((percentile_cont(0.99) WITHIN GROUP (ORDER BY us) / 1000)::numeric, 1) AS p99,
       round(max(us) / 1000.0, 1) AS max
FROM stress_tx_parsed
GROUP BY ord, workload, variant
ORDER BY ord, variant DESC;

\echo
\echo '== pgbench runs'
SELECT variant, tps, deadlocks AS pg_stat_deadlocks, mix_exit AS pgbench_exit, reconciler_exit
FROM stress_run ORDER BY variant DESC;

CREATE TABLE stress_check AS
SELECT 1 AS ord, 'pending dispatch pages walk ix_gateway_messages_pending_order without a sort' AS name,
       CASE WHEN count(*) = 6 AND bool_and(stress_walks(plan, 'ix_gateway_messages_pending_order')
                                           AND NOT stress_has(plan, 'Sort')) THEN 'PASS' ELSE 'FAIL' END AS result,
       count(*) || ' after-fix probes: first, mid and generic mid page in both phases' AS detail
FROM stress_plan
WHERE variant = 'after' AND label IN ('pending-page-1', 'pending-page-mid', 'pending-page-mid-generic')
UNION ALL
SELECT 2, 'recent chat history walks ix_chat_messages_agent_conv_created without a sort',
       CASE WHEN count(*) = 2 AND bool_and(stress_walks(plan, 'ix_chat_messages_agent_conv_created')
                                           AND NOT stress_has(plan, 'Sort')) THEN 'PASS' ELSE 'FAIL' END,
       count(*) || ' after-fix probes'
FROM stress_plan WHERE variant = 'after' AND label = 'history'
UNION ALL
SELECT 3, 'agent gateway listing walks ix_gateway_messages_agent_created without a scan or sort',
       CASE WHEN count(*) = 2 AND bool_and(stress_walks(plan, 'ix_gateway_messages_agent_created')
                                           AND NOT stress_has(plan, 'Sort') AND NOT stress_has(plan, 'Seq Scan'))
            THEN 'PASS' ELSE 'FAIL' END,
       count(*) || ' after-fix probes'
FROM stress_plan WHERE variant = 'after' AND label = 'gateway-list'
UNION ALL
SELECT 4, 'every fixed probe reads fewer buffers after the fix',
       CASE WHEN count(*) = 10 AND bool_and(a.buffers < b.buffers) THEN 'PASS' ELSE 'FAIL' END,
       string_agg(format('%s %s: %s -> %s', phase, label, b.buffers, a.buffers), '; ' ORDER BY phase DESC, label)
FROM stress_plan_summary AS b
JOIN stress_plan_summary AS a USING (phase, label)
WHERE b.variant = 'before' AND a.variant = 'after'
  AND label IN ('pending-page-1', 'pending-page-mid', 'pending-page-mid-generic', 'history', 'gateway-list')
UNION ALL
SELECT 5, 'no deadlocks or other failed transactions after the fix',
       CASE WHEN count(us) > 0 AND count(*) = count(us) THEN 'PASS' ELSE 'FAIL' END,
       format('%s completed, %s failed', count(us), count(*) - count(us))
FROM stress_tx_parsed WHERE variant = 'after'
UNION ALL
SELECT 6, 'deadlocks reproduced before the fix', 'INFO',
       format('%s of %s transactions deadlocked', count(*) FILTER (WHERE t = 'deadlock'), count(*))
FROM stress_tx_parsed WHERE variant = 'before';

\echo
\echo '== Checks'
SELECT name, result, detail FROM stress_check ORDER BY ord;
