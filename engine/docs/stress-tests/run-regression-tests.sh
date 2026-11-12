#!/usr/bin/env bash
# Runs the tests that pin the stress-test fixes against a disposable PostgreSQL container. See README.md.
#
# Usage (from anywhere): docs/stress-tests/run-regression-tests.sh
# Needs uv (as for any engine test run). Environment: STRESS_TEST_CONTAINER (default maraclaw-stress-tests),
# STRESS_IMAGE (default postgres:18.6-alpine).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE="$(cd "$HERE/../.." && pwd)"
# shellcheck source=lib.sh
. "$HERE/lib.sh"

CONTAINER="${STRESS_TEST_CONTAINER:-maraclaw-stress-tests}"
TESTS=(
    tests/test_chat_submission_scope_locks_postgres.py
    tests/test_gateway_lane_reads_postgres.py
    tests/test_chat_history_postgres.py
    tests/test_agent_stop_enforcement.py
)

command -v uv >/dev/null 2>&1 || stress_die "uv is not on PATH"
stress_preflight "$CONTAINER" 10

OUTPUT="$(mktemp "${TMPDIR:-/tmp}/maraclaw-stress-tests.XXXXXX")"
# shellcheck disable=SC2329 # invoked by the EXIT trap
cleanup() {
    local status=$?
    docker stop "$CONTAINER" >/dev/null 2>&1 || true
    rm -f "$OUTPUT"
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

stress_start_postgres "$CONTAINER" -p 127.0.0.1::5432
PORT="$(docker port "$CONTAINER" 5432/tcp | head -1 | sed 's/.*://')"
DSN="postgresql://stress:stress@127.0.0.1:${PORT}/stress"

# The DSN variables the engine-ci lifecycle-postgres job sets; each fixture creates and drops its own schema.
export AGENT_SETUP_TEST_DATABASE_URL="$DSN" AGENT_SETUP_TEST_DATABASE_DISPOSABLE=1 MARACLAW_TEST_POSTGRES_DSN="$DSN"
export CHAT_SUBMISSION_TEST_DATABASE_URL="$DSN" BOOTSTRAP_TEST_DATABASE_URL="$DSN" UV_LOCKED=1

cd "$ENGINE"
status=0
uv run --extra dev python -m pytest "${TESTS[@]}" -q -p no:cacheprovider -rs 2>&1 | tee "$OUTPUT" || status=$?
if grep -Eq '[0-9]+ skipped' "$OUTPUT"; then
    echo "[stress] FAIL: tests were skipped, so they did not run against PostgreSQL" >&2
    status=1
fi
exit "$status"
