#!/usr/bin/env bash
# Build and run the MaraClaw admin console using this package's Dockerfile.
#
# Usage:
#   ./start-from-docker.sh                         # build + run (foreground)
#   ./start-from-docker.sh --build-only            # build without running
#   ./start-from-docker.sh --no-build              # run the existing image
#   ./start-from-docker.sh -- <extra docker run args>
#
# Environment overrides (explicit shell environment only; no .env is loaded):
#   IMAGE_NAME          image tag             (default: maraclaw-web-a:local)
#   CONTAINER_NAME      container name        (default: maraclaw-web-a)
#   PORT                host port -> 8080     (default: 5174)
#   DOCKER_NETWORK      shared engine network (default: maraclaw_network)
#   VITE_API_BASE_URL   public build-time URL (default: empty, same-origin)
#   DETACH=1            run with -d --restart unless-stopped (default: --rm)
#   PULL=1              build with --pull
#   CONTAINER_LOG_MAX_SIZE, CONTAINER_LOG_MAX_FILE
#                       json-file/local log rotation (default: 10m, 3). Added only when the effective driver (an
#                       explicit --log-driver in the extra args, else the daemon default) is json-file or local.
#
# API upstream: the engine runs with alias maraclaw-engine on the same
# DOCKER_NETWORK (default maraclaw_network). Caddy proxies /api/ to it and
# resolves the name per connection, so start order does not matter. This
# launcher supplies --network (manual runs need --network maraclaw_network).
# Only the public API URL is passed to the build; no runtime env is forwarded.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

IMAGE_NAME="${IMAGE_NAME:-maraclaw-web-a:local}"
CONTAINER_NAME="${CONTAINER_NAME:-maraclaw-web-a}"
PORT="${PORT:-5174}"
DOCKER_NETWORK="${DOCKER_NETWORK:-maraclaw_network}"
CONTAINER_LOG_MAX_SIZE="${CONTAINER_LOG_MAX_SIZE:-10m}"
CONTAINER_LOG_MAX_FILE="${CONTAINER_LOG_MAX_FILE:-3}"
if ! [[ "$CONTAINER_LOG_MAX_SIZE" =~ ^[1-9][0-9]*[kKmMgG]?$ ]] || ! [[ "$CONTAINER_LOG_MAX_FILE" =~ ^[1-9][0-9]*$ ]]; then
    echo "[docker-run] ERROR: CONTAINER_LOG_MAX_SIZE must be a size such as 10m and CONTAINER_LOG_MAX_FILE a positive count" >&2
    exit 1
fi

DO_BUILD=1
DO_RUN=1
EXTRA_RUN_ARGS=()

while [ $# -gt 0 ]; do
    case "$1" in
        --build-only) DO_RUN=0; shift ;;
        --no-build)   DO_BUILD=0; shift ;;
        --)           shift; EXTRA_RUN_ARGS+=("$@"); break ;;
        *)            EXTRA_RUN_ARGS+=("$1"); shift ;;
    esac
done

if command -v docker >/dev/null 2>&1; then
    ENGINE="docker"
elif command -v podman >/dev/null 2>&1; then
    ENGINE="podman"
else
    echo "[docker-run] ERROR: neither 'docker' nor 'podman' found in PATH" >&2
    exit 1
fi
echo "[docker-run] Using container engine: $ENGINE"

if [ "$DO_BUILD" = "1" ]; then
    BUILD_ARGS=(--build-arg "VITE_API_BASE_URL=${VITE_API_BASE_URL:-}")
    if [ "${PULL:-0}" = "1" ]; then
        BUILD_ARGS+=(--pull)
    fi

    echo "[docker-run] Building image: $IMAGE_NAME"
    "$ENGINE" build "${BUILD_ARGS[@]}" -t "$IMAGE_NAME" -f Dockerfile .
fi

if [ "$DO_RUN" = "0" ]; then
    echo "[docker-run] --build-only, skipping run"
    exit 0
fi

if ! "$ENGINE" network inspect "$DOCKER_NETWORK" >/dev/null 2>&1; then
    echo "[docker-run] Creating Docker network: $DOCKER_NETWORK"
    "$ENGINE" network create "$DOCKER_NETWORK" >/dev/null
fi

# Match the full name, not a substring or Docker's name-filter regex.
if "$ENGINE" ps -a --format '{{.Names}}' | grep -Fxq -- "$CONTAINER_NAME"; then
    echo "[docker-run] Removing existing container: $CONTAINER_NAME"
    "$ENGINE" rm -f "$CONTAINER_NAME" >/dev/null
fi

RUN_ARGS=(
    --name "$CONTAINER_NAME"
    --network "$DOCKER_NETWORK"
    -p "${PORT}:8080"
)

# json-file never rotates, so bound it. Only json-file and local understand max-size; other drivers (journald, awslogs,
# ...) reject it and `docker run` fails. The effective driver is the last --log-driver in the extra run arguments,
# else the daemon default; when it cannot be determined, pass no size options. Extra --log-opt arguments still win.
LOG_DRIVER=""
previous=""
if [ "${#EXTRA_RUN_ARGS[@]}" -gt 0 ]; then
    for arg in "${EXTRA_RUN_ARGS[@]}"; do
        case "$arg" in
            --log-driver=*) LOG_DRIVER="${arg#--log-driver=}" ;;
            *) if [ "$previous" = "--log-driver" ]; then LOG_DRIVER="$arg"; fi ;;
        esac
        previous="$arg"
    done
fi
if [ -z "$LOG_DRIVER" ]; then
    LOG_DRIVER="$("$ENGINE" info --format '{{.LoggingDriver}}' 2>/dev/null || true)"
fi
case "$LOG_DRIVER" in
    json-file|local)
        RUN_ARGS+=(--log-opt "max-size=$CONTAINER_LOG_MAX_SIZE" --log-opt "max-file=$CONTAINER_LOG_MAX_FILE")
        ;;
esac

if [ "${DETACH:-0}" = "1" ]; then
    RUN_ARGS+=(-d --restart unless-stopped)
else
    RUN_ARGS+=(--rm)
fi

echo "[docker-run] Starting container $CONTAINER_NAME on http://localhost:${PORT}"
# Bash 3.2 with nounset cannot expand an empty array; keep this branch explicit.
if [ "${#EXTRA_RUN_ARGS[@]}" -gt 0 ]; then
    exec "$ENGINE" run "${RUN_ARGS[@]}" "${EXTRA_RUN_ARGS[@]}" "$IMAGE_NAME"
else
    exec "$ENGINE" run "${RUN_ARGS[@]}" "$IMAGE_NAME"
fi
