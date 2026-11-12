# shellcheck shell=bash
# Helpers shared by run-stress.sh and run-regression-tests.sh: a host disk guard and a disposable PostgreSQL.
#
# Containers always run with --rm. The postgres image declares a volume, so a container without --rm leaves an
# anonymous volume behind on every run. Durability is off: never point real data at these containers.

STRESS_IMAGE="${STRESS_IMAGE:-postgres:18.6-alpine}"

stress_die() {
    echo "[stress] ERROR: $*" >&2
    exit 1
}

# Free GB on the filesystem behind Docker's storage. On macOS that is the host volume holding Docker Desktop's
# sparse VM disk; when it fills, the VM goes read-only and every container on it fails.
stress_host_free_gb() {
    local path="/var/lib/docker"
    [ "$(uname -s)" = "Darwin" ] && path="$HOME"
    [ -d "$path" ] || path="/"
    df -Pk "$path" | awk 'NR==2 {print int($4 / 1048576)}'
}

stress_preflight() {
    local name="$1" min_free_gb="$2" free
    command -v docker >/dev/null 2>&1 || stress_die "docker is not on PATH"
    docker info >/dev/null 2>&1 || stress_die "the Docker daemon is not reachable"
    if docker inspect "$name" >/dev/null 2>&1; then
        stress_die "a container named $name already exists; stop it (docker stop $name) or set another name"
    fi
    free="$(stress_host_free_gb)"
    [ "$free" -ge "$min_free_gb" ] || stress_die "only ${free} GB free on the Docker host disk; need ${min_free_gb} GB"
}

# stress_start_postgres NAME [docker run options...]: starts the container and waits until it accepts TCP.
stress_start_postgres() {
    local name="$1" ready=0
    shift
    docker run -d --rm --name "$name" --shm-size=1g "$@" \
        -e POSTGRES_USER=stress -e POSTGRES_PASSWORD=stress -e POSTGRES_DB=stress \
        "$STRESS_IMAGE" \
        -c fsync=off -c full_page_writes=off -c synchronous_commit=off \
        -c wal_level=minimal -c max_wal_senders=0 -c max_wal_size=1GB \
        -c max_connections=300 -c shared_buffers=512MB -c work_mem=8MB -c maintenance_work_mem=256MB >/dev/null
    # The image runs a socket-only bootstrap server first; probe TCP so this waits for the real one.
    for _ in $(seq 1 60); do
        if docker exec "$name" pg_isready -q -h 127.0.0.1 -U stress -d stress 2>/dev/null; then
            ready=1
            break
        fi
        sleep 1
    done
    [ "$ready" -eq 1 ] || stress_die "$name did not accept connections within 60s"
}

# stress_watchdog NAME STOP_BELOW_GB: stops the container if the host disk runs low. Run it in the background.
stress_watchdog() {
    while docker inspect "$1" >/dev/null 2>&1; do
        if [ "$(stress_host_free_gb)" -lt "$2" ]; then
            echo "[stress] host free space fell below $2 GB; stopping $1" >&2
            docker stop "$1" >/dev/null 2>&1 || true
            return
        fi
        sleep 5
    done
}
