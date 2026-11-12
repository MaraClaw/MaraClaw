#!/usr/bin/env bash
# Reproduces the chat-turn database stress test in a disposable PostgreSQL container. See README.md.
#
# Usage (from anywhere):
#   docs/stress-tests/run-stress.sh            # recorded scale: 2,000 agents, 10M rows per history table
#   docs/stress-tests/run-stress.sh --quick    # 200 agents, 1M rows per history table
#   docs/stress-tests/run-stress.sh --keep     # leave the container running for inspection
#
# Environment overrides:
#   STRESS_AGENTS        agents (and owners)                       (default 2000; 200 with --quick)
#   STRESS_HISTORY       completed turns and chat messages per agent  (default 5000)
#   STRESS_BACKLOG       extra pending turns per agent, backlog phase (default 81)
#   STRESS_CLIENTS       pgbench clients                           (default 64)
#   STRESS_THREADS       pgbench threads                           (default 8)
#   STRESS_SECONDS       pgbench duration per variant              (default 60)
#   STRESS_CONTAINER     container name                            (default maraclaw-stress)
#   STRESS_IMAGE         PostgreSQL image                          (default postgres:18.6-alpine)
#   STRESS_OUT_DIR       where report.txt, plans.txt and pgbench output go (default: a new temp directory)
#   STRESS_MIN_FREE_GB   refuse to start below this host free space (default 20; 10 with --quick)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE="$(cd "$HERE/../.." && pwd)"
# shellcheck source=lib.sh
. "$HERE/lib.sh"

QUICK=0
KEEP=0
for arg in "$@"; do
    case "$arg" in
        --quick) QUICK=1 ;;
        --keep) KEEP=1 ;;
        -h | --help)
            sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) stress_die "unknown option: $arg (see --help)" ;;
    esac
done

if [ "$QUICK" = 1 ]; then
    STRESS_AGENTS="${STRESS_AGENTS:-200}"
    MIN_FREE_GB="${STRESS_MIN_FREE_GB:-10}"
else
    STRESS_AGENTS="${STRESS_AGENTS:-2000}"
    MIN_FREE_GB="${STRESS_MIN_FREE_GB:-20}"
fi
STRESS_HISTORY="${STRESS_HISTORY:-5000}"
STRESS_BACKLOG="${STRESS_BACKLOG:-81}"
STRESS_CLIENTS="${STRESS_CLIENTS:-64}"
STRESS_THREADS="${STRESS_THREADS:-8}"
STRESS_SECONDS="${STRESS_SECONDS:-60}"
for knob in STRESS_AGENTS STRESS_HISTORY STRESS_BACKLOG STRESS_CLIENTS STRESS_THREADS STRESS_SECONDS MIN_FREE_GB; do
    [[ "${!knob}" =~ ^[1-9][0-9]*$ ]] || stress_die "$knob must be a positive integer, not '${!knob}'"
done
CONTAINER="${STRESS_CONTAINER:-maraclaw-stress}"
OUT_DIR="${STRESS_OUT_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/maraclaw-stress.XXXXXX")}"
mkdir -p "$OUT_DIR"

stress_preflight "$CONTAINER" "$MIN_FREE_GB"

WATCHDOG=""
# shellcheck disable=SC2329 # invoked by the EXIT trap
cleanup() {
    local status=$?
    [ -z "$WATCHDOG" ] || kill "$WATCHDOG" 2>/dev/null || true
    if [ "$KEEP" = 1 ]; then
        echo "[stress] kept $CONTAINER: docker exec -it $CONTAINER psql -U stress; remove it with docker stop $CONTAINER" >&2
    else
        docker stop "$CONTAINER" >/dev/null 2>&1 || true
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

echo "[stress] starting $CONTAINER ($STRESS_IMAGE); results go to $OUT_DIR"
stress_start_postgres "$CONTAINER"
stress_watchdog "$CONTAINER" 5 &
WATCHDOG=$!

docker exec "$CONTAINER" mkdir -p /stress/out
docker cp "$HERE/container/." "$CONTAINER:/stress/" >/dev/null
docker cp "$ENGINE/scripts/schema_baseline.sql" "$CONTAINER:/stress/schema_baseline.sql" >/dev/null

status=0
docker exec \
    -e STRESS_AGENTS="$STRESS_AGENTS" -e STRESS_HISTORY="$STRESS_HISTORY" -e STRESS_BACKLOG="$STRESS_BACKLOG" \
    -e STRESS_CLIENTS="$STRESS_CLIENTS" -e STRESS_THREADS="$STRESS_THREADS" -e STRESS_SECONDS="$STRESS_SECONDS" \
    "$CONTAINER" sh /stress/stress.sh || status=$?

docker cp "$CONTAINER:/stress/out/." "$OUT_DIR/" >/dev/null 2>&1 || echo "[stress] could not copy /stress/out" >&2
echo "[stress] report: $OUT_DIR/report.txt  plans: $OUT_DIR/plans.txt  pgbench output and logs: $OUT_DIR"
exit "$status"
