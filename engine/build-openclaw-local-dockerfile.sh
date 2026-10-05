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

TENCENTDB_PLUGIN_VERSION="${TENCENTDB_PLUGIN_VERSION:-1.0.1}"
OPENCLAW_VERSION="${OPENCLAW_VERSION:-2026.9.8}"
OPENCLAW_SHA256="${OPENCLAW_SHA256:-sha256:317e0a58db32b386e01187fe9c5c4de541f4ce6d815657bf79a102609b81752a}"
GOGCLI_VERSION="${GOGCLI_VERSION:-0.43.0}"
GOGCLI_SHA256="${GOGCLI_SHA256:-sha256:f66e3c9ab7664b7633d57d2d5303e0db75deb4045e1b32c3493c0d8ba68a70f7}"
DOCKER_PLATFORM="${DOCKER_PLATFORM:-linux/arm64}"

if [ "$DOCKER_PLATFORM" != "linux/arm64" ]; then
    echo "[openclaw-build] gogcli is pinned to the linux/arm64 release; set DOCKER_PLATFORM=linux/arm64 or add per-arch gogcli artifacts before building $DOCKER_PLATFORM." >&2
    exit 2
fi

echo "[openclaw-build] Building openclaw:local for $DOCKER_PLATFORM with OpenClaw $OPENCLAW_VERSION and TencentDB plugin $TENCENTDB_PLUGIN_VERSION"
docker buildx build \
    --platform "$DOCKER_PLATFORM" \
    -f Dockerfile.openclaw \
    --build-arg TENCENTDB_PLUGIN_VERSION="$TENCENTDB_PLUGIN_VERSION" \
    --build-arg OPENCLAW_VERSION="$OPENCLAW_VERSION" \
    --build-arg OPENCLAW_SHA256="$OPENCLAW_SHA256" \
    --build-arg GOGCLI_VERSION="$GOGCLI_VERSION" \
    --build-arg GOGCLI_SHA256="$GOGCLI_SHA256" \
    -t openclaw:local \
    --load \
    .
echo "[openclaw-build] Successfully built local Docker image openclaw:local"
