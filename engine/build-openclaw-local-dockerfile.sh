#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

ENV_FILE="${ENV_FILE:-.env}"
if [ -f "$ENV_FILE" ]; then
    echo "[openclaw-build] Sourcing $ENV_FILE"
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi

LOCAL_IMAGE="${MARACLAW_OPENCLAW_IMAGE:-openclaw:local}"
OPENCLAW_VERSION="${OPENCLAW_VERSION:-2026.10.7}"
OPENCLAW_SHA256="${OPENCLAW_SHA256:-sha256:45dc6dadab3ca4e5286262226d6b61554309435a5e83b5d25c803a3a12ef2f81}"
GOGCLI_VERSION="${GOGCLI_VERSION:-0.47.0}"
GOGCLI_SHA256="${GOGCLI_SHA256:-sha256:90d236ab91c6dae7139bfe0508eab94a8b4323238b62fa29e6cf479a352576f2}"
DOCKER_PLATFORM="${DOCKER_PLATFORM:-linux/arm64}"

if [ "$DOCKER_PLATFORM" != "linux/arm64" ]; then
    echo "[openclaw-build] gogcli is pinned to the linux/arm64 release; set DOCKER_PLATFORM=linux/arm64 or add per-arch gogcli artifacts before building $DOCKER_PLATFORM." >&2
    exit 2
fi

echo "[openclaw-build] Building $LOCAL_IMAGE for $DOCKER_PLATFORM with OpenClaw $OPENCLAW_VERSION"
docker buildx build \
    --platform "$DOCKER_PLATFORM" \
    -f Dockerfile.openclaw \
    --build-arg OPENCLAW_VERSION="$OPENCLAW_VERSION" \
    --build-arg OPENCLAW_SHA256="$OPENCLAW_SHA256" \
    --build-arg GOGCLI_VERSION="$GOGCLI_VERSION" \
    --build-arg GOGCLI_SHA256="$GOGCLI_SHA256" \
    -t "$LOCAL_IMAGE" \
    --load \
    .
echo "[openclaw-build] Successfully built local Docker image $LOCAL_IMAGE"
