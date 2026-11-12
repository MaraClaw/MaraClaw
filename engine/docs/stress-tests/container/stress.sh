#!/bin/sh
# Runs inside the disposable PostgreSQL container that run-stress.sh starts. Never point it at a real database.
#
# Loads scripts/schema_baseline.sql without the three fix indexes, seeds it, measures the pre-fix SQL, reruns the
# baseline over the seeded data (what bootstrap does to an existing database), measures the fixed SQL and writes
# report.txt, plans.txt and the pgbench output to /stress/out. Exits non-zero when a report check fails.
set -eu

DIR=/stress
OUT=$DIR/out
AGENTS=${STRESS_AGENTS:-2000}
HISTORY=${STRESS_HISTORY:-5000}
BACKLOG=${STRESS_BACKLOG:-81}
CLIENTS=${STRESS_CLIENTS:-64}
THREADS=${STRESS_THREADS:-8}
DURATION=${STRESS_SECONDS:-60}
FIX_INDEXES="'ix_gateway_messages_pending_order', 'ix_gateway_messages_agent_created', 'ix_chat_messages_agent_conv_created'"
export PGUSER=stress PGDATABASE=stress PGOPTIONS='-c client_min_messages=warning'

log() { printf '[stress %s] %s\n' "$(date +%H:%M:%S)" "$*"; }
sql() { psql -X -q -v ON_ERROR_STOP=1 "$@"; }
value() { psql -X -A -t -v ON_ERROR_STOP=1 -c "$1"; }
meta() { sql -c "INSERT INTO stress_meta (key, value) VALUES ('$1', $2)"; }

wait_all() {
    failed=0
    for pid in "$@"; do wait "$pid" || failed=1; done
    return "$failed"
}

# Backends flush their statistics as they exit; wait for every pgbench session to go.
wait_idle() {
    tries=0
    while [ "$(value "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()
                      AND backend_type = 'client backend' AND pid <> pg_backend_pid()")" != 0 ]; do
        tries=$((tries + 1))
        [ "$tries" -le 60 ] || { log "pgbench sessions did not end"; return 1; }
        sleep 1
    done
}

measure() {
    variant=$1
    log "$variant: EXPLAIN probes, steady state"
    sql -v variant="$variant" -v phase=steady -v agents="$AGENTS" -f "$DIR/explain.sql"
    log "$variant: EXPLAIN probes, with $BACKLOG more pending turns per agent"
    sql -v add=1 -v agents="$AGENTS" -v backlog="$BACKLOG" -f "$DIR/backlog.sql"
    sql -v variant="$variant" -v phase=backlog -v agents="$AGENTS" -f "$DIR/explain.sql"
    sql -v add=0 -f "$DIR/backlog.sql"

    log "$variant: pgbench, $CLIENTS clients for ${DURATION}s, reconciler about once a second"
    cd "$OUT"
    deadlocks_before=$(value "SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()")
    pgbench -n -M extended -c 1 -j 1 -T "$((DURATION + 2))" --rate=1 -l --log-prefix="$variant-rec" \
        -f "$DIR/bench-reconcile-$variant.sql" > "$variant-rec.txt" 2>&1 &
    reconciler=$!
    mix_exit=0
    pgbench -n -M extended -c "$CLIENTS" -j "$THREADS" -T "$DURATION" -P 20 --failures-detailed \
        -l --log-prefix="$variant-mix" -D agents="$AGENTS" \
        -f "$DIR/bench-intake-$variant.sql@40" -f "$DIR/bench-claim-settle.sql@40" -f "$DIR/bench-history.sql@20" \
        > "$variant-mix.txt" 2>&1 || mix_exit=$?
    reconciler_exit=0
    wait "$reconciler" || reconciler_exit=$?
    wait_idle
    deadlocks_after=$(value "SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()")
    for kind in mix rec; do
        set -- "$variant-$kind".[0-9]*
        [ -e "$1" ] || { log "pgbench wrote no $kind log; see $OUT/$variant-$kind.txt"; exit 1; }
        cat "$@" | sql -c "\\copy stress_tx (line) FROM pstdin"
        sql -c "UPDATE stress_tx SET variant = '$variant', kind = '$kind' WHERE variant IS NULL"
    done
    tps=$(awk '/^tps = / {print $3}' "$variant-mix.txt")
    sql -c "INSERT INTO stress_run VALUES ('$variant', ${tps:-0}, $((deadlocks_after - deadlocks_before)),
            $mix_exit, $reconciler_exit)"
    cd "$DIR"
    grep -E '^(number of (transactions actually processed|failed transactions|deadlock failures)|tps)' \
        "$OUT/$variant-mix.txt" | sed "s/^/[stress] $variant: /"

    log "$variant: restoring the steady state"
    sql -f "$DIR/reset.sql"
}

mkdir -p "$OUT"
started=$(date +%s)

log "schema: scripts/schema_baseline.sql without the three fix indexes"
sql -f "$DIR/schema_baseline.sql"
sql -c "DROP INDEX ix_gateway_messages_pending_order, ix_gateway_messages_agent_created,
        ix_chat_messages_agent_conv_created"
sql -f "$DIR/setup.sql"
meta 'PostgreSQL' "'$(value 'SHOW server_version')'"
meta 'agents' "'$AGENTS'"
meta 'completed turns and chat messages per agent' "'$HISTORY'"
meta 'backlog phase: extra pending turns per agent' "'$BACKLOG'"
meta 'pgbench' "'$CLIENTS clients, $THREADS threads, ${DURATION}s'"

log "seed: $AGENTS agents with $HISTORY completed turns and chat messages each"
t0=$(date +%s)
sql -v agents="$AGENTS" -f "$DIR/seed-base.sql"
pids=""
for part in gateway chat history; do
    sql -v agents="$AGENTS" -v history="$HISTORY" -f "$DIR/seed-$part.sql" &
    pids="$pids $!"
done
# shellcheck disable=SC2086 # one pid per word
wait_all $pids
log "seed: rebuilding secondary indexes"
pids=""
for name in $(value "SELECT name FROM stress_index_defs ORDER BY name"); do
    sql -v name="$name" -f "$DIR/seed-index.sql" &
    pids="$pids $!"
done
# shellcheck disable=SC2086
wait_all $pids
sql -v agents="$AGENTS" -f "$DIR/seed-steady.sql"
vacuumdb -q -z -j 4
meta 'seed seconds' "'$(($(date +%s) - t0))'"
meta 'rows' "(SELECT format('%s gateway_messages (%s pending, %s delivered), %s chat_messages, %s chat_history_changes',
    count(*), count(*) FILTER (WHERE status = 'pending'), count(*) FILTER (WHERE status = 'delivered'),
    (SELECT count(*) FROM chat_messages), (SELECT count(*) FROM chat_history_changes)) FROM gateway_messages)"
meta 'database size after seeding' "pg_size_pretty(pg_database_size(current_database()))"

[ "$(value "SELECT count(*) FROM pg_indexes WHERE indexname IN ($FIX_INDEXES)")" = 0 ]
measure before

log "upgrade: rerunning scripts/schema_baseline.sql over the seeded database"
t0=$(date +%s)
sql -f "$DIR/schema_baseline.sql"
meta 'baseline rerun seconds (builds the fix indexes)' "'$(($(date +%s) - t0))'"
[ "$(value "SELECT count(*) FROM pg_indexes WHERE indexname IN ($FIX_INDEXES)")" = 3 ] || {
    log "the baseline did not create the three fix indexes"
    exit 1
}
meta 'fix index sizes' "(SELECT string_agg(format('%s %s', indexname, pg_size_pretty(pg_relation_size(indexname::regclass))),
    ', ' ORDER BY indexname) FROM pg_indexes WHERE indexname IN ($FIX_INDEXES))"
measure after

sql -f "$DIR/report.sql" > "$OUT/report.txt"
cat "$OUT/report.txt"
value "SELECT string_agg(format(E'=== %s / %s / %s (%s pending)\n%s', variant, phase, label, pending, plan_text),
       E'\n' ORDER BY variant DESC, phase DESC, label) FROM stress_plan" > "$OUT/plans.txt"
failed=$(value "SELECT count(*) FROM stress_check WHERE result = 'FAIL'")
log "finished in $(($(date +%s) - started))s with $failed failed checks"
[ "$failed" = 0 ]
