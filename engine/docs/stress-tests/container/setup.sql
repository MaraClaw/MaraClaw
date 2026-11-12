-- Bookkeeping for one stress run, created beside the application tables in the disposable database.

CREATE TABLE stress_meta (ord serial PRIMARY KEY, key text NOT NULL, value text NOT NULL);

-- One recorded EXPLAIN per variant (before | after), phase (steady | backlog) and probe.
CREATE TABLE stress_plan (
    variant text NOT NULL,
    phase text NOT NULL,
    label text NOT NULL,
    pending bigint NOT NULL,
    plan jsonb NOT NULL,
    plan_text text NOT NULL,
    PRIMARY KEY (variant, phase, label)
);

-- Raw pgbench per-transaction log lines: client_id transaction_no time script_no epoch epoch_us [schedule_lag].
CREATE TABLE stress_tx (variant text, kind text, line text NOT NULL);

CREATE TABLE stress_run (
    variant text PRIMARY KEY,
    tps numeric NOT NULL,
    deadlocks bigint NOT NULL,
    mix_exit integer NOT NULL,
    reconciler_exit integer NOT NULL
);

-- Runs p_sql once to warm the cache, then records its EXPLAIN (ANALYZE, BUFFERS) as JSON (measured) and as text.
CREATE FUNCTION stress_explain(p_variant text, p_phase text, p_label text, p_sql text) RETURNS void
LANGUAGE plpgsql AS $fn$
DECLARE
    doc json;
    line text;
    rendered text := '';
BEGIN
    EXECUTE 'EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ' || p_sql INTO doc;
    EXECUTE 'EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ' || p_sql INTO doc;
    FOR line IN EXECUTE 'EXPLAIN (ANALYZE, BUFFERS) ' || p_sql LOOP
        rendered := rendered || line || E'\n';
    END LOOP;
    INSERT INTO stress_plan
    VALUES (p_variant, p_phase, p_label, (SELECT count(*) FROM gateway_messages WHERE status = 'pending'),
            doc::jsonb, rendered);
END
$fn$;
