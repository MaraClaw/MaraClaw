#!/usr/bin/env bash
# Disposable local PostgreSQL for the opt-in `*_postgres.py` tests and load tests.
#
# The postgres image declares VOLUME /var/lib/postgresql, so a bare
# `docker run postgres` leaves an anonymous volume behind on every run. This
# wrapper always uses `--rm`, which removes that volume when the container
# stops. Durability is off: never point real data at it.
#
# Usage (from engine/):
#   scripts/pg-throwaway.sh start [--watchdog]   # start, wait until ready, print DSN
#   scripts/pg-throwaway.sh dsn                  # print DSN
#   scripts/pg-throwaway.sh stop                 # stop; container and volume are removed
#
# Environment overrides:
#   PG_NAME      container name            (default: maraclaw-pg-throwaway)
#   PG_PORT      loopback host port        (default: a free port chosen by Docker)
#   PG_IMAGE     image                     (default: postgres:18.6-alpine)
#   PG_MIN_FREE_GB   refuse to start below this host free space   (default: 10)
#   PG_STOP_FREE_GB  --watchdog stops the container below this    (default: 5)
set -euo pipefail

PG_NAME="${PG_NAME:-maraclaw-pg-throwaway}"
PG_PORT="${PG_PORT:-}"
PG_IMAGE="${PG_IMAGE:-postgres:18.6-alpine}"
PG_MIN_FREE_GB="${PG_MIN_FREE_GB:-10}"
PG_STOP_FREE_GB="${PG_STOP_FREE_GB:-5}"

dsn() {
    local port
    port="$(docker port "$PG_NAME" 5432/tcp | head -1 | sed 's/.*://')"
    echo "postgresql://maraclaw:maraclaw@127.0.0.1:${port}/maraclaw_test"
}

# Free GB on the filesystem that backs Docker's storage. On macOS that is the
# host volume holding Docker Desktop's sparse VM disk; the VM's own `df`
# over-reports, so the host is the real limit.
host_free_gb() {
    local path="/var/lib/docker"
    [ "$(uname -s)" = "Darwin" ] && path="$HOME"
    [ -d "$path" ] || path="/"
    df -Pk "$path" | awk 'NR==2 {print int($4 / 1048576)}'
}

watchdog() {
    while docker inspect "$PG_NAME" >/dev/null 2>&1; do
        if [ "$(host_free_gb)" -lt "$PG_STOP_FREE_GB" ]; then
            echo "[pg-throwaway] host free space below ${PG_STOP_FREE_GB} GB; stopping $PG_NAME" >&2
            docker stop "$PG_NAME" >/dev/null 2>&1 || true
            return
        fi
        sleep 5
    done
}

case "${1:-}" in
    start)
        free="$(host_free_gb)"
        if [ "$free" -lt "$PG_MIN_FREE_GB" ]; then
            echo "[pg-throwaway] ERROR: only ${free} GB free on the Docker host disk (need ${PG_MIN_FREE_GB})." >&2
            exit 1
        fi
        docker run -d --rm --name "$PG_NAME" \
            -p "127.0.0.1:${PG_PORT}:5432" \
            -e POSTGRES_USER=maraclaw -e POSTGRES_PASSWORD=maraclaw -e POSTGRES_DB=maraclaw_test \
            "$PG_IMAGE" \
            -c fsync=off -c full_page_writes=off -c synchronous_commit=off \
            -c wal_level=minimal -c max_wal_senders=0 -c max_wal_size=1GB \
            -c max_connections=300 >/dev/null
        # The image runs a socket-only bootstrap server first; probe TCP so we wait for the real one.
        ready=0
        for _ in $(seq 1 60); do
            if docker exec "$PG_NAME" pg_isready -q -h 127.0.0.1 -U maraclaw -d maraclaw_test 2>/dev/null; then
                ready=1
                break
            fi
            sleep 0.5
        done
        if [ "$ready" -ne 1 ]; then
            echo "[pg-throwaway] ERROR: $PG_NAME did not become ready in 30s; stopping it." >&2
            docker logs --tail 20 "$PG_NAME" >&2 2>&1 || true
            docker stop "$PG_NAME" >/dev/null 2>&1 || true
            exit 1
        fi
        if [ "${2:-}" = "--watchdog" ]; then
            nohup "$0" _watchdog >/dev/null 2>&1 &
            echo "[pg-throwaway] watchdog pid $! stops $PG_NAME below ${PG_STOP_FREE_GB} GB free" >&2
        fi
        dsn
        ;;
    dsn) dsn ;;
    stop) docker stop "$PG_NAME" >/dev/null && echo "[pg-throwaway] stopped $PG_NAME (volume removed)" >&2 ;;
    _watchdog) watchdog ;;
    *) sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2 ;;
esac
