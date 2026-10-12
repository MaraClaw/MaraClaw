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
OPENCLAW_VERSION="${OPENCLAW_VERSION:-2026.10.1}"
OPENCLAW_SHA256="${OPENCLAW_SHA256:-sha256:6af9925274c60309763e1c6c9bcc9afcc3c59775c0ac4c403fe5460fe0190136}"
GOGCLI_VERSION="${GOGCLI_VERSION:-0.44.0}"
GOGCLI_SHA256="${GOGCLI_SHA256:-sha256:0346149cac6630996c69911ac7366c3c1a36d8835b23e985e90c3f8d25c7ab65}"
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
