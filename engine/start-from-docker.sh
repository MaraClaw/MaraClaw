#!/usr/bin/env bash
# Build and run the MaraClaw backend from the project Dockerfile.
#
# .env handling:
#   This script sources `.env` (if present) into its own shell BEFORE applying
#   defaults, so `.env` can set IMAGE_NAME, PORT, APT_MIRROR, MARACLAW_PIP_*,
#   DETACH, PULL, etc. — same vars listed below as "Environment overrides".
#   Each variable defined in `.env` is then forwarded to the container via
#   `-e KEY` (docker reads the value from the host env, so JSON-quoted values
#   like CORS_ORIGINS survive correctly — unlike `--env-file`, which copies
#   lines literally including the wrapping quotes).
#
# Usage:
#   ./start-from-docker.sh                        # build + run (foreground)
#   ./start-from-docker.sh --build-only           # just build the image
#   ./start-from-docker.sh --no-build             # skip build, just run
#   ./start-from-docker.sh -- <extra docker run args>
#
# Environment overrides (set in .env OR on the command line):
#   IMAGE_NAME           image tag                       (default: maraclaw-backend:local)
#   CONTAINER_NAME       container name                  (default: maraclaw-backend)
#   PORT                 host port mapped to 8000        (default: 8000)
#   DATA_DIR             host dir mounted at /data       (default: ./.docker-data)
#   APT_MIRROR           build-time apt mirror           (e.g. mirrors.ustc.edu.cn)
#   MARACLAW_PIP_INDEX_URL, MARACLAW_PIP_TRUSTED_HOST      (build-time pip mirror)
#   ENV_FILE             path to env file to source      (default: .env)
#   DETACH=1             run detached (docker -d)
#   PULL=1               docker pull base image before build

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Variables to NEVER forward into the container even if present in .env
# (they'd corrupt the container's shell environment).
ENV_DENYLIST_REGEX='^(PATH|HOME|USER|SHELL|PWD|OLDPWD|TMPDIR|LANG|LC_ALL|TERM|HOSTNAME|SHLVL|DISPLAY|_)$'

# ---- Load .env (host-side) -------------------------------------------------
ENV_FILE="${ENV_FILE:-.env}"
ENV_KEYS=""
if [ -f "$ENV_FILE" ]; then
    echo "[docker-run] Sourcing $ENV_FILE into host shell"
    set -a
    # shellcheck disable=SC1090
    . "./$ENV_FILE"
    set +a

    # Collect KEY names declared in the file (KEY=... lines only, no comments).
    # We forward these to the container via -e KEY.
    ENV_KEYS=$(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$ENV_FILE" \
                   | sed -E 's/=.*//' \
                   | grep -vE "$ENV_DENYLIST_REGEX" \
                   | sort -u)
fi

# ---- Defaults (applied AFTER sourcing .env so .env can override) -----------
IMAGE_NAME="${IMAGE_NAME:-maraclaw-backend:local}"
CONTAINER_NAME="${CONTAINER_NAME:-maraclaw-backend}"
PORT="${PORT:-8000}"
DATA_DIR="${DATA_DIR:-$SCRIPT_DIR/.docker-data}"

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

# Pick container engine: docker, else podman.
if command -v docker >/dev/null 2>&1; then
    ENGINE="docker"
elif command -v podman >/dev/null 2>&1; then
    ENGINE="podman"
else
    echo "[docker-run] ERROR: neither 'docker' nor 'podman' found in PATH" >&2
    exit 1
fi
echo "[docker-run] Using container engine: $ENGINE"

# Podman on macOS runs inside a Linux VM that does not share /Volumes/ paths
# by default.  If DATA_DIR is under /Volumes/ and is still the script default,
# redirect it to $HOME/.maraclaw-data which the VM always shares.  If the user
# explicitly set a /Volumes/ path we warn and exit with remediation steps.
if [ "$ENGINE" = "podman" ] && [[ "$DATA_DIR" == /Volumes/* ]]; then
    if [ "$DATA_DIR" = "$SCRIPT_DIR/.docker-data" ]; then
        DATA_DIR="$HOME/.maraclaw-data"
        echo "[docker-run] podman: /Volumes/ not shared with VM; redirecting DATA_DIR to $DATA_DIR"
    else
        echo "[docker-run] ERROR: DATA_DIR=$DATA_DIR is under /Volumes/ which is not shared with the podman VM." >&2
        echo "[docker-run] Fix: podman machine stop && podman machine set --volume \"$DATA_DIR:$DATA_DIR\" && podman machine start" >&2
        exit 1
    fi
fi

# ---- Build -----------------------------------------------------------------
if [ "$DO_BUILD" = "1" ]; then
    BUILD_ARGS=()
    [ -n "${APT_MIRROR:-}" ]                && BUILD_ARGS+=(--build-arg "APT_MIRROR=$APT_MIRROR")
    [ -n "${MARACLAW_PIP_INDEX_URL:-}" ]     && BUILD_ARGS+=(--build-arg "MARACLAW_PIP_INDEX_URL=$MARACLAW_PIP_INDEX_URL")
    [ -n "${MARACLAW_PIP_TRUSTED_HOST:-}" ]  && BUILD_ARGS+=(--build-arg "MARACLAW_PIP_TRUSTED_HOST=$MARACLAW_PIP_TRUSTED_HOST")
    [ "${PULL:-0}" = "1" ]                  && BUILD_ARGS+=(--pull)

    echo "[docker-run] Building image: $IMAGE_NAME"
    if [ "${#BUILD_ARGS[@]}" -gt 0 ]; then
        "$ENGINE" build "${BUILD_ARGS[@]}" -t "$IMAGE_NAME" -f Dockerfile .
    else
        "$ENGINE" build -t "$IMAGE_NAME" -f Dockerfile .
    fi
fi

if [ "$DO_RUN" = "0" ]; then
    echo "[docker-run] --build-only, skipping run"
    exit 0
fi

# ---- Run -------------------------------------------------------------------
mkdir -p "$DATA_DIR"

# Remove any previous container with the same name.
if "$ENGINE" ps -a --format '{{.Names}}' | grep -Fxq "$CONTAINER_NAME"; then
    echo "[docker-run] Removing existing container: $CONTAINER_NAME"
    "$ENGINE" rm -f "$CONTAINER_NAME" >/dev/null
fi

RUN_ARGS=(
    --name "$CONTAINER_NAME"
    -p "${PORT}:8000"
    -v "${DATA_DIR}:/data"
    # AGENT_DATA_DIR is pinned to the mount path; never let .env override it.
    -e "AGENT_DATA_DIR=/data/agents"
)

# Forward each .env-declared variable from host env into the container.
# Using `-e KEY` (no =value) makes docker read the value from this shell's
# environment, where bash has already stripped wrapping quotes correctly.
if [ -n "$ENV_KEYS" ]; then
    forwarded_count=0
    while IFS= read -r key; do
        [ -z "$key" ] && continue
        # Skip AGENT_DATA_DIR (already pinned above).
        [ "$key" = "AGENT_DATA_DIR" ] && continue
        RUN_ARGS+=(-e "$key")
        forwarded_count=$((forwarded_count + 1))
    done <<< "$ENV_KEYS"
    echo "[docker-run] Forwarding $forwarded_count env vars from $ENV_FILE"
fi

if [ "${DETACH:-0}" = "1" ]; then
    RUN_ARGS+=(-d --restart unless-stopped)
else
    RUN_ARGS+=(--rm -it)
fi

echo "[docker-run] Starting container $CONTAINER_NAME on http://localhost:${PORT}"
if [ "${#EXTRA_RUN_ARGS[@]}" -gt 0 ]; then
    exec "$ENGINE" run "${RUN_ARGS[@]}" "${EXTRA_RUN_ARGS[@]}" "$IMAGE_NAME"
else
    exec "$ENGINE" run "${RUN_ARGS[@]}" "$IMAGE_NAME"
fi
