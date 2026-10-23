#!/usr/bin/env bash
# Build and run the MaraClaw end-user UI using this package's Dockerfile.
#
# Usage:
#   ./start-from-docker.sh                         # build + run (foreground)
#   ./start-from-docker.sh --build-only            # build without running
#   ./start-from-docker.sh --no-build              # run the existing image
#   ./start-from-docker.sh -- <extra docker run args>
#
# Environment overrides (explicit shell environment only; no .env is loaded):
#   IMAGE_NAME          image tag             (default: maraclaw-web-e:local)
#   CONTAINER_NAME      container name        (default: maraclaw-web-e)
#   PORT                host port -> 8080     (default: 5173)
#   DOCKER_NETWORK      shared engine network (default: maraclaw_network)
#   VITE_API_BASE_URL   public build-time URL (default: empty, same-origin)
#   DETACH=1            run with -d --restart unless-stopped (default: --rm)
#   PULL=1              build with --pull
#
# API prerequisite: start ../engine/start-from-docker.sh first, on the same
# DOCKER_NETWORK with alias maraclaw-engine. nginx resolves maraclaw-engine:8000
# at startup and proxies /api/, /ws/ and /p/ there, even with a custom API URL.
# Only the public API URL is passed to the build; no runtime env is forwarded.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

IMAGE_NAME="${IMAGE_NAME:-maraclaw-web-e:local}"
CONTAINER_NAME="${CONTAINER_NAME:-maraclaw-web-e}"
PORT="${PORT:-5173}"
DOCKER_NETWORK="${DOCKER_NETWORK:-maraclaw_network}"

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
